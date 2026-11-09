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
from typing import Dict, List

from .config import DB_PATH, USER_DB_PATH

# 查询级超时（毫秒）：防止慢查询把服务挂住
QUERY_TIMEOUT_MS = 10_000
# 网页/接口最多返回的行数（明细查询 sql_guard 已补 LIMIT 100，这里再兜一道）
MAX_RETURN_ROWS = 1000
# 超时检查粒度：SQLite 每执行约 N 条虚拟机指令回调一次（过小有额外开销）
_PROGRESS_STEPS = 10_000


class ReadonlyExecError(Exception):
    """只读执行失败（SQL 语法错误、超时等），message 可直接展示。"""


def open_readonly_connection() -> sqlite3.Connection:
    """
    以严格只读方式打开 SQLite：
      - file:...?mode=ro 是 SQLite 官方只读模式，任何写操作直接报错；
      - uri=True 才会识别 file: 协议；
      - 路径统一转成正斜杠，避免 Windows 反斜杠在 URI 里被当转义符。

    用户上传表（xlsx/csv 导入）放在独立的 USER_DB_PATH，以只读方式
    ATTACH 成 `userdb` 附加库：主库仍只读，附加库也 mode=ro，
    生成的 SQL 需用 userdb."表名" 限定，守卫/LIMIT/超时路径不变。
    """
    uri = "file:%s?mode=ro" % DB_PATH.resolve().as_posix()
    con = sqlite3.connect(uri, uri=True, timeout=QUERY_TIMEOUT_MS / 1000)
    # 再兜一层：本连接上不允许任何隐式写（触发器之类），双保险
    con.execute("PRAGMA query_only = ON")
    if USER_DB_PATH.exists():
        try:
            user_uri = "file:%s?mode=ro" % USER_DB_PATH.resolve().as_posix()
            con.execute("ATTACH DATABASE ? AS userdb", (user_uri,))
        except sqlite3.Error:
            # 附加库不可用不应阻断业务库问数；用户表查询会自然报 no such table
            pass
    return con


def execute_select(sql: str) -> Dict[str, object]:
    """
    执行一条已通过 sql_guard 检查的只读 SELECT。

    成功返回：
      {
        "columns": ["customer_name", "amount"],
        "rows": [["海思半导体", 124623.6], ...],
        "row_count": 12
      }
    失败抛 ReadonlyExecError（调用方负责把 message 返回给用户/记录日志）。
    """
    con = open_readonly_connection()
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
        result = execute_select(guarded.sql)
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
