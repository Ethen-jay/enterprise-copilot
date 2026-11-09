# -*- coding: utf-8 -*-
"""
executor.py —— 只读 SQL 执行器（阶段 C）

职责非常单一：
  - 用 SQLite 的只读 URI（mode=ro）打开数据库，从连接层保证写不进去；
  - 执行 sql_guard 放行后的 SELECT；
  - 把结果统一成 {"columns": [...], "rows": [[...]], "row_count": N}。

本模块不直接调用大模型、不做选表；FastAPI 层（阶段 D）把
schema_linker -> llm_client -> sql_guard -> executor 串起来。

命令行自测（在项目根目录运行，绕过 LLM 直接验证只读链路）：
    python -m app.executor "SELECT COUNT(*) AS n FROM fact_work_order WHERE lot_status='在制'"
"""

import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import DB_PATH, USER_DB_PATH
from .userdb import NotConnectedError, current_path

# 查询级超时（毫秒）：防止慢查询把服务挂住
QUERY_TIMEOUT_MS = 10_000
# 网页/接口最多返回的行数（明细查询 sql_guard 已补 LIMIT 100，这里再兜一道）
MAX_RETURN_ROWS = 1000
# 超时检查粒度：SQLite 每执行约 N 条虚拟机指令回调一次（过小有额外开销）
_PROGRESS_STEPS = 10_000


class ReadonlyExecError(Exception):
    """只读执行失败（SQL 语法错误、超时等），message 可直接展示。"""


def open_readonly_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    以严格只读方式打开 SQLite：
      - file:...?mode=ro 是 SQLite 官方只读模式，任何写操作直接报错；
      - uri=True 才会识别 file: 协议；
      - 路径统一转成正斜杠，避免 Windows 反斜杠在 URI 里被当转义符。

    db_path 省略时取当前请求绑定的数据源（userdb.set_current）。
    未接入任何数据库时抛 NotConnectedError，调用方据此返回
    “请先接入数据库”，不回落默认库、也不报堆栈。

    仅当主库是内置示例库时，才把历史遗留的 xlsx/csv 导入库
    （USER_DB_PATH）以只读方式 ATTACH 成 `userdb`；用户接入自己的库时
    不挂载，避免跨用户看到别人上传的表格。
    """
    path = Path(db_path) if db_path else current_path()
    if path is None:
        raise NotConnectedError("请先接入数据库")
    if not path.exists():
        raise NotConnectedError("数据源文件不存在，请重新接入")
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    con = sqlite3.connect(uri, uri=True, timeout=QUERY_TIMEOUT_MS / 1000)
    # 再兜一层：本连接上不允许任何隐式写（触发器之类），双保险
    con.execute("PRAGMA query_only = ON")
    try:
        is_builtin = path.resolve() == DB_PATH.resolve()
    except OSError:
        is_builtin = False
    if is_builtin and USER_DB_PATH.exists() and USER_DB_PATH.resolve() != path.resolve():
        try:
            user_uri = "file:%s?mode=ro" % USER_DB_PATH.resolve().as_posix()
            con.execute("ATTACH DATABASE ? AS userdb", (user_uri,))
        except sqlite3.Error:
            # 附加库不可用不应阻断业务库问数；用户表查询会自然报 no such table
            pass
    return con


def execute_select(sql: str, db_path: Optional[Path] = None) -> Dict[str, object]:
    """
    执行一条已通过 sql_guard 检查的只读 SELECT。

    成功返回：
      {
        "columns": ["customer_name", "amount"],
        "rows": [["海思半导体", 124623.6], ...],
        "row_count": 12
      }
     失败抛 ReadonlyExecError（调用方负责把 message 返回给用户/记录日志）；
     未接入数据库抛 NotConnectedError（调用方转成“请先接入数据库”提示）。
    """
    con = open_readonly_connection(db_path)
    try:
        # 真·执行超时：connect(timeout=) 只控制等锁，拦不住笛卡尔积这类慢查询；
        # progress_handler 在 SQLite 虚拟机内定期回调，超时返回非 0 即中断执行。
        deadline = time.monotonic() + QUERY_TIMEOUT_MS / 1000.0
        con.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_STEPS
        )
        cursor = con.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        raw_rows = cursor.fetchmany(MAX_RETURN_ROWS + 1)
        truncated = False
        if len(raw_rows) > MAX_RETURN_ROWS:
            raw_rows = raw_rows[:MAX_RETURN_ROWS]
            truncated = True
        rows: List[list] = [list(r) for r in raw_rows]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    except sqlite3.OperationalError as e:
        if "interrupt" in str(e).lower():
            raise ReadonlyExecError(
                "查询超时（超过 %d 秒），请缩小时间范围或增加过滤条件"
                % (QUERY_TIMEOUT_MS // 1000)
            ) from e
        # 例如 no such column、syntax error —— 原文对排查很有用，直接带上
        raise ReadonlyExecError("SQL 执行失败：%s" % e) from e
    except sqlite3.Error as e:
        raise ReadonlyExecError("SQL 执行失败：%s" % e) from e
    finally:
        con.close()


# ----------------------------------------------------------------------
# 命令行自测
# ----------------------------------------------------------------------
def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print('用法：python app/executor.py "SELECT ..."')
        return 2

    # 命令行直跑时也先过一遍守卫，保证自测链路与线上一致
    from .sql_guard import check_sql

    guarded = check_sql(argv[1])
    if not guarded.ok:
        print("守卫拒绝：%s" % guarded.reason)
        return 1
    if guarded.changed:
        print("（守卫已自动补 LIMIT %d）" % 100)

    try:
        result = execute_select(guarded.sql, db_path=DB_PATH)
    except ReadonlyExecError as e:
        print(e)
        return 1

    print("列：%s" % result["columns"])
    print("返回 %d 行：" % result["row_count"])
    for row in result["rows"][:10]:
        print("  ", row)
    if result["truncated"]:
        print("  ...（超过 %d 行已截断）" % MAX_RETURN_ROWS)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
