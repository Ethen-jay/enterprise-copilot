# -*- coding: utf-8 -*-
"""schema_scan.py —— 对“当前用户所连数据库”做真实 schema 扫描

状态条上的表数量、数据快照日期一律由这里扫描得出，不再写死。
扫描对象是当前用户实际接入的库（见 userdb.py），扫描内容包括：

  1. 表 / 视图清单：逐表 PRAGMA table_info 取列名与声明类型；
  2. 纳入 / 排除判定（结果与原因都返回给前端，不是只给一个总数）：
       - sqlite_*            系统表               → 排除
       - _meta_*             系统登记表           → 排除
       - 无列定义            无业务字段           → 排除
       - 列全部是技术列      无业务字段           → 排除
     其余表纳入，附带行数（超过 20 万行只标记下限，避免大表拖慢首次扫描）。
  3. 快照日期：对纳入表的日期列执行 MAX(日期) 取全局最大值，
     并返回来源（表.列）；库内没有任何可识别日期列时回退为文件修改日期，
     两种情况都在 snapshot_source 里说明，前端可直接展示。

缓存：
  进程内按“库文件路径”缓存扫描结果，指纹 = 文件大小:修改时间(ns)。
  文件变了指纹自动失效；用户点“刷新”可 force=True 强制重扫。
  首次扫描耗时随表数量线性增长（PRAGMA + COUNT + MAX），结果里回传
  duration_ms，便于观察对首次请求的影响。
"""

import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

ROWS_CAP = 200_000

_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_DATE_TYPE_RE = re.compile(r"(date|time)", re.I)
_DATE_NAME_RE = re.compile(r"(^|_)(date|dt|day)$|(_date|_time|_dt)$", re.I)
_TECH_COL_RE = re.compile(
    r"^(id|_id|rowid|uuid|guid|created_at|updated_at|created|updated|ts|timestamp)$", re.I
)

_cache: Dict[str, dict] = {}
_lock = threading.RLock()


def _fingerprint(path: Path) -> str:
    st = path.stat()
    return "%d:%d" % (st.st_size, st.st_mtime_ns)


def _fingerprint_text(path: Path) -> str:
    try:
        return _fingerprint(path)
    except OSError:
        return ""


def _is_system_table(name: str) -> Optional[str]:
    lower = name.lower()
    if lower.startswith("sqlite_"):
        return "SQLite 系统表"
    if lower.startswith("_meta"):
        return "系统登记表（_meta_*）"
    return None


def _is_business_column(name: str, declared_type: str) -> bool:
    """是否存在业务字段：既不是 ID/时间戳这类纯技术列，也不是空列名。"""
    return bool(name) and not _TECH_COL_RE.match(name)


def _normalize_date(value) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    m = _DATE_RE.match(text)
    if not m:
        return None
    return m.group(0).replace("/", "-")


def _date_candidates(columns: List[dict]) -> List[str]:
    """挑出可做 MAX() 的日期列：声明类型优先，其次列名模式。"""
    typed = [c["name"] for c in columns if _DATE_TYPE_RE.search(c.get("type") or "")]
    named = [c["name"] for c in columns if _DATE_NAME_RE.search(c["name"] or "")]
    ordered: List[str] = []
    for name in typed + named:
        if name not in ordered:
            ordered.append(name)
    return ordered[:3]


def _scan_table(con: sqlite3.Connection, name: str, kind: str) -> dict:
    cols = con.execute('PRAGMA table_info("%s")' % name.replace('"', '""')).fetchall()
    columns = [
        {"name": c[1], "type": (c[2] or "").upper() or "TEXT", "pk": bool(c[5])}
        for c in cols
    ]
    info = {
        "name": name,
        "kind": kind,
        "columns": columns,
        "rows": 0,
        "rows_capped": False,
        "included": True,
        "reason": "",
        "date_columns": _date_candidates(columns),
    }
    system_reason = _is_system_table(name)
    if system_reason:
        info.update(included=False, reason=system_reason)
        return info
    if not columns:
        info.update(included=False, reason="无业务字段（无列定义）")
        return info
    if not any(_is_business_column(c["name"], c["type"]) for c in columns):
        info.update(included=False, reason="无业务字段（仅主键/时间戳等技术列）")
        return info
    try:
        row = con.execute(
            'SELECT COUNT(*) FROM (SELECT 1 FROM "%s" LIMIT %d)'
            % (name.replace('"', '""'), ROWS_CAP + 1)
        ).fetchone()
        info["rows"] = int(row[0]) if row else 0
        info["rows_capped"] = info["rows"] > ROWS_CAP
    except sqlite3.Error:
        info["rows"] = 0
    return info


def _snapshot(con: sqlite3.Connection, tables: List[dict]) -> tuple:
    best = None
    best_source = ""
    errors = 0
    for table in tables:
        if not table["included"]:
            continue
        for col in table["date_columns"]:
            sql = 'SELECT MAX("%s") FROM "%s"' % (
                col.replace('"', '""'), table["name"].replace('"', '""')
            )
            try:
                value = con.execute(sql).fetchone()
            except sqlite3.Error:
                errors += 1
                continue
            raw = value[0] if value else None
            if raw is None:
                continue
            normalized = _normalize_date(raw)
            if not normalized:
                errors += 1
                continue
            if best is None or normalized > best:
                best = normalized
                best_source = "%s.%s" % (table["name"], col)
    if best:
        return best, "MAX(%s) -> %s" % (best_source, best), True
    return None, "", False


def scan(path: Path, force: bool = False) -> dict:
    """扫描指定库文件；返回可直接给前端展示的结构（带缓存）。"""
    path = Path(path)
    key = str(path.resolve())
    fingerprint = _fingerprint_text(path)
    if not force:
        with _lock:
            cached = _cache.get(key)
        if cached and cached.get("fingerprint") == fingerprint:
            result = dict(cached)
            result["cached"] = True
            return result

    started = time.monotonic()
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = [_scan_table(con, name, kind) for name, kind in rows]
        snapshot_date, snapshot_source, snapshot_dynamic = _snapshot(con, tables)
    finally:
        con.close()

    included = [t for t in tables if t["included"]]
    excluded = [
        {"name": t["name"], "kind": t["kind"], "reason": t["reason"]}
        for t in tables if not t["included"]
    ]
    if snapshot_date:
        snapshot_note = "取库内业务日期列的最大值"
    else:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        snapshot_date = mtime.strftime("%Y-%m-%d")
        snapshot_source = "库文件修改时间（未发现业务日期列）"
        snapshot_note = "未发现业务日期列，回退为库文件修改日期"
    result = {
        "fingerprint": fingerprint,
        "cached": False,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "total_tables": len(tables),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "tables": included,
        "excluded": excluded,
        "snapshot_date": snapshot_date,
        "snapshot_source": snapshot_source,
        "snapshot_dynamic": snapshot_dynamic,
        "snapshot_note": snapshot_note,
    }
    with _lock:
        _cache[key] = result
    return dict(result)


def invalidate(path: Path) -> None:
    with _lock:
        _cache.pop(str(Path(path).resolve()), None)
