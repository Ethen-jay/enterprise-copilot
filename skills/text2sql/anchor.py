# -*- coding: utf-8 -*-
"""
anchor.py —— 运行时动态基准日（生产化改造：数据滚动更新）

背景：ANCHOR 原来写死 2026-09-24（静态快照的数据窗末日）。数据改为每日
ETL 增量同步后，固定锚点会漏掉新数据；锚定系统时钟 date('now') 又会
查空（运行日晚于数据窗）。因此改为运行时查库取 MAX(业务日期)：
数据最新到哪天，"本月/最近一周/今天"就锚到哪天，锚点自己跟着数据走。

探针口径（三条红线，实测踩过坑）：
  1. 只探测"实际发生"类时间列（出站/出货/电测/客诉/下单），
     禁止计划/到期类——fact_receivable.due_date 分布到 2026-11-23，
     会把锚点拉到未来；
  2. fact_process_record 用主记录口径（is_rework_pass=0）：
     实测 9-25 仅剩 1 行凌晨 00:42 的返工重过站记录（过程记录，
     不代表产出数据新鲜度），排除后 MAX 恰为 2026-09-24；
  3. 各探针取 MAX(date(col)) 再取整体最大：任何核心域有新数据，
     锚点就跟上（数据残缺日天然不会成为锚点，因为当天没数据）。

可靠性：
  - 进程内 TTL 缓存（ANCHOR_TTL_SECONDS，默认 300 秒；0 = 每次现查）；
  - 单个探针查询失败自动跳过；全部失败回退 .env 的 ANCHOR_DATE 兜底，
    服务可用性优先，绝不因锚点探测失败挂掉主链路。

命令行自测（在 monorepo 根目录运行）：
    python -m skills.text2sql.anchor
"""

import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .config import ANCHOR_DATE, ANCHOR_TTL_SECONDS, DB_PATH, SNAPSHOT_DATE
from . import schema_scan
from .executor import open_readonly_connection
from .userdb import current_path

# 探针清单：(表名, 业务时间列, 附加 WHERE 或 None)
# 新增事实表时在这里补一行；只放"实际发生"类日期，禁止计划/到期类。
ANCHOR_PROBES: List[Tuple[str, str, Optional[str]]] = [
    ("fact_process_record", "out_time", "is_rework_pass = 0"),  # 生产出站（主记录口径）
    ("fact_shipment", "ship_date", None),                       # 实际出货
    ("fact_etest", "test_time", None),                          # 电测
    ("fact_customer_complaint", "complaint_date", None),        # 客诉
    ("fact_sales_order", "order_date", None),                   # 下单
]

DATE_RE_MIN = "1000-01-01"  # MAX(date(col)) 的合法性下限（防脏数据把锚点拉到远古）


@dataclass(frozen=True)
class AnchorInfo:
    date: str      # YYYY-MM-DD
    dynamic: bool  # True=运行时探测；False=静态兜底（.env ANCHOR_DATE）
    source: str    # 探测来源描述，日志/审计用


# ----------------------------------------------------------------------
# 1. 现查库（无缓存）
# ----------------------------------------------------------------------
def query_anchor_date(db_path: Optional[Path] = None) -> AnchorInfo:
    """
    对内置示例库按 ANCHOR_PROBES 探测；对用户接入的库则按扫描出的
    日期列取 MAX（见 schema_scan），口径统一为“库内真实数据的最大日期”。

    任何单探针失败（表被删、列改名、库打不开）只跳过不抛错；
    全部失败时回退 config.ANCHOR_DATE（dynamic=False），保证服务可用。
    """
    path = Path(db_path) if db_path else (current_path() or DB_PATH)
    try:
        is_builtin = path.resolve() == DB_PATH.resolve()
    except OSError:
        is_builtin = False

    if not is_builtin:
        return _query_user_anchor(path)

    results = []   # [(date, 来源描述)]
    errors = []    # [失败原因]
    try:
        con = open_readonly_connection(path)
    except sqlite3.Error as e:
        return AnchorInfo(date=ANCHOR_DATE, dynamic=False,
                          source="fallback（数据库无法打开：%s）" % e)
    try:
        for table, col, extra_where in ANCHOR_PROBES:
            sql = 'SELECT MAX(date("%s")) FROM "%s"' % (col, table)
            if extra_where:
                sql += " WHERE %s" % extra_where
            try:
                row = con.execute(sql).fetchone()
            except sqlite3.Error as e:
                errors.append("%s.%s: %s" % (table, col, e))
                continue
            value = row[0] if row else None
            if value and value >= DATE_RE_MIN:
                results.append((value, "%s.%s" % (table, col)))
            elif value:
                errors.append("%s.%s: 非法日期 %s" % (table, col, value))
    finally:
        con.close()

    if not results:
        detail = "；".join(errors) if errors else "所有探针表均为空"
        print("[Anchor] ⚠ 动态锚点探测失败（%s），回退静态 ANCHOR_DATE=%s"
              % (detail, ANCHOR_DATE))
        return AnchorInfo(date=ANCHOR_DATE, dynamic=False,
                          source="fallback（%s）" % detail)

    best = max(results)                                  # 按日期字符串比较即字典序
    hits = "、".join("%s=%s" % (src, d) for d, src in sorted(results, reverse=True))
    return AnchorInfo(date=best[0], dynamic=True,
                      source="MAX(业务日期) -> %s（%s）" % (best[0], hits))


def _query_user_anchor(path: Path) -> AnchorInfo:
    """用户接入库：用 schema 扫描出的日期列取 MAX，扫描结果本身带缓存。"""
    try:
        info = schema_scan.scan(path)
    except (OSError, sqlite3.Error) as e:
        return AnchorInfo(date=ANCHOR_DATE, dynamic=False,
                          source="fallback（数据源无法扫描：%s）" % e)
    date = info.get("snapshot_date") or ANCHOR_DATE
    dynamic = bool(info.get("snapshot_dynamic"))
    source = "schema 扫描 -> %s（%s）" % (date, info.get("snapshot_source") or "无日期列")
    return AnchorInfo(date=date, dynamic=dynamic, source=source)


# ----------------------------------------------------------------------
# 2. TTL 缓存入口（schema_linker / llm_client / service 都从这里取）
# ----------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache: dict = {"info": None, "ts": 0.0, "key": ""}


def _path_key(path: Optional[Path]) -> str:
    target = Path(path) if path else (current_path() or DB_PATH)
    try:
        return str(target.resolve())
    except OSError:
        return str(target)


def get_anchor_date(force_refresh: bool = False, db_path: Optional[Path] = None) -> AnchorInfo:
    """
    带进程内 TTL 缓存的锚点入口（缓存按数据源路径隔离）：
      - 缓存未过期直接复用（数据按天滚动，默认 5 分钟足够跟进）；
      - force_refresh=True 或 TTL=0 时每次现查；
      - 线程安全（FastAPI 同步端点跑在线程池里）。
    """
    key = _path_key(db_path)
    if ANCHOR_TTL_SECONDS <= 0 or force_refresh:
        info = query_anchor_date(db_path)
        with _cache_lock:
            _cache.update(info=info, ts=time.time(), key=key)
        return info

    with _cache_lock:
        if (_cache["info"] is not None and _cache["key"] == key
                and time.time() - _cache["ts"] < ANCHOR_TTL_SECONDS):
            return _cache["info"]
    info = query_anchor_date(db_path)
    with _cache_lock:
        _cache.update(info=info, ts=time.time(), key=key)
    return info


def get_snapshot_date(db_path: Optional[Path] = None) -> str:
    """
    数据快照日（状态条展示口径）：
      - 内置示例库显式配置了 SNAPSHOT_DATE -> 固定值（人工演示场景）；
      - 其余情况 -> 跟随该库真实数据的最大业务日期（用户接入库走 schema 扫描）。
    """
    target = Path(db_path) if db_path else (current_path() or DB_PATH)
    try:
        is_builtin = target.resolve() == DB_PATH.resolve()
    except OSError:
        is_builtin = False
    if SNAPSHOT_DATE and is_builtin:
        return SNAPSHOT_DATE
    return get_anchor_date(db_path=target).date


# ----------------------------------------------------------------------
# 3. 命令行自测
# ----------------------------------------------------------------------
def main(argv: List[str]) -> int:
    info = get_anchor_date(force_refresh=True)
    print("动态锚点探测：")
    print("  锚点日期 ：%s" % info.date)
    print("  是否动态 ：%s" % ("是（运行时 MAX(业务日期)）" if info.dynamic
                              else "否（静态兜底 .env ANCHOR_DATE）"))
    print("  来源明细 ：%s" % info.source)
    print("  账龄快照日：%s" % get_snapshot_date())
    return 0 if info.dynamic else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
