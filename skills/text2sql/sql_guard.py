# -*- coding: utf-8 -*-
"""
sql_guard.py —— SQL 只读守卫（阶段 C）

大模型生成的 SQL 不可信，执行前必须过这一关。本模块只做“静态检查”，
不改写业务口径，唯一会改动 SQL 的地方是：非聚合查询且没写 LIMIT 时，
自动在末尾补 LIMIT 100，防止一次性捞出几万行撑爆网页/Token。

放行规则（全部满足才放行）：
  1. 只能有 1 条语句（拒绝 ; 拼接的多语句）；
  2. 语句必须是 SELECT 或 WITH ... SELECT（CTE）；
  3. 不允许出现任何写操作/危险操作关键字：
     INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/REPLACE/PRAGMA/ATTACH/...
  4. 非聚合查询自动补 LIMIT 100。

注意：这是“第二道防线”。第一道防线是 executor 用 SQLite 只读 URI 打开库，
即使守卫被绕过，数据库文件本身也写不进去。

命令行自测：
    python app/sql_guard.py "SELECT * FROM dim_customer"
"""

import re
import sys
from dataclasses import dataclass
from typing import Optional

import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Statement

# 非聚合明细查询的兜底行数
DEFAULT_LIMIT = 100

# 一律拒绝的关键字（写操作 + SQLite 管理类/附加库类操作）
# 用大写单词做精确匹配，避免误伤列名里的子串
FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE",
    "TRUNCATE", "MERGE", "GRANT", "REVOKE", "COMMIT", "ROLLBACK",
    "PRAGMA", "ATTACH", "DETACH", "VACUUM", "REINDEX", "ANALYZE",
    "BEGIN", "SAVEPOINT", "RELEASE",
}

# 聚合函数：出现任一即认为是“聚合查询”，不补 LIMIT
AGG_FUNC_RE = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT)\s*\(", re.I)
# 已有限制
HAS_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.I)
# 末尾 LIMIT 追加位置：去掉结尾分号/空白后追加
TRAILING_SEMI_RE = re.compile(r";+\s*$")


@dataclass
class GuardResult:
    ok: bool
    sql: str            # 放行时为（可能补过 LIMIT 的）最终 SQL
    reason: str         # 拒绝原因；放行时为 "ok"
    changed: bool = False  # 是否自动补过 LIMIT


def _statement_keyword(stmt: Statement) -> Optional[str]:
    """取语句的第一个有意义关键字（DML/DDL 引导词），大写返回。"""
    for token in stmt.flatten():
        if token.is_whitespace or token.ttype in T.Comment:
            continue
        # 第一个有意义的词
        if token.ttype in T.Keyword or token.ttype in T.DML or token.ttype in T.DDL:
            return token.normalized.upper()
        # 也可能被识别成 Name 等，直接取裸词
        word = token.normalized.upper().strip("(\"'`")
        return word if word else None
    return None


def check_sql(raw_sql: str) -> GuardResult:
    """
    检查一条大模型生成的 SQL。

    返回 GuardResult：
      ok=True  -> sql 字段是可以交给 executor 的安全 SQL
      ok=False -> reason 字段写明拒绝原因（直接展示给调用方/日志）
    """
    if raw_sql is None or not raw_sql.strip():
        return GuardResult(False, "", "空 SQL")

    text = raw_sql.strip()

    # ---- 规则 1：多语句检查 ----
    # sqlparse.split 能识别引号/注释里的分号，不会误切
    statements = [s for s in sqlparse.parse(text) if s.value.strip()]
    if len(statements) != 1:
        return GuardResult(False, text, "只允许单条查询，检测到 %d 条语句" % len(statements))

    stmt = statements[0]

    # ---- 规则 2：首词必须是 SELECT / WITH ----
    first_kw = _statement_keyword(stmt)
    if first_kw not in ("SELECT", "WITH"):
        return GuardResult(False, text, "只允许 SELECT/WITH 查询，实际以 %s 开头" % first_kw)

    # WITH 开头时，必须能在后面找到 SELECT；拒绝 "WITH x AS (...) DELETE..." 这类拼法
    if first_kw == "WITH":
        if not re.search(r"\bSELECT\b", stmt.value, re.I):
            return GuardResult(False, text, "WITH 语句中未找到 SELECT")

    # ---- 规则 3：扫描禁用关键字（基于词法 token，不做朴素子串匹配）----
    hit = set()
    for token in stmt.flatten():
        if token.ttype in (T.Keyword, T.Keyword.DML, T.Keyword.DDL):
            word = token.normalized.upper()
            if word in FORBIDDEN_KEYWORDS:
                hit.add(word)
    if hit:
        return GuardResult(False, text, "检测到禁止的操作关键字：%s" % "、".join(sorted(hit)))

    # ---- 规则 4：非聚合且无 LIMIT 的明细查询，补 LIMIT ----
    final_sql = TRAILING_SEMI_RE.sub("", text).strip()
    changed = False
    is_aggregate = bool(AGG_FUNC_RE.search(final_sql))
    has_limit = bool(HAS_LIMIT_RE.search(final_sql))
    if not is_aggregate and not has_limit:
        final_sql = "%s\nLIMIT %d" % (final_sql, DEFAULT_LIMIT)
        changed = True

    return GuardResult(True, final_sql, "ok", changed=changed)


# ----------------------------------------------------------------------
# 命令行自测
# ----------------------------------------------------------------------
def _self_test() -> int:
    cases = [
        # (SQL, 期望是否放行)
        ("SELECT * FROM dim_customer", True),                       # 明细 -> 补 LIMIT
        ("SELECT COUNT(*) FROM fact_work_order WHERE lot_status='在制'", True),
        ("WITH t AS (SELECT lot_id FROM fact_work_order) SELECT * FROM t", True),
        ("SELECT * FROM dim_customer;", True),                      # 尾分号允许
        ("SELECT * FROM dim_customer; DROP TABLE dim_customer;", False),
        ("DELETE FROM dim_customer", False),
        ("UPDATE dim_customer SET customer_name='x'", False),
        ("PRAGMA table_info(dim_customer)", False),
        ("ATTACH DATABASE 'x.db' AS x", False),
        ("WITH x AS (SELECT 1) DELETE FROM dim_customer", False),
        ("", False),
    ]
    all_pass = True
    for sql, expect_ok in cases:
        r = check_sql(sql)
        mark = "✓" if r.ok == expect_ok else "✗ 期望放行=%s" % expect_ok
        if r.ok != expect_ok:
            all_pass = False
        print("%s ok=%-5s %s" % (mark, r.ok, ("补LIMIT " if r.changed else "") + (r.reason if not r.ok else "")))
        if r.ok and r.changed:
            print("        -> %s" % r.sql.replace("\n", " "))
    print("\n全部通过" if all_pass else "\n有用例未通过！")
    return 0 if all_pass else 1


if __name__ == "__main__":
    if len(sys.argv) > 1:
        result = check_sql(" ".join(sys.argv[1:]))
        print("ok=%s reason=%s changed=%s" % (result.ok, result.reason, result.changed))
        print(result.sql)
        sys.exit(0 if result.ok else 1)
    sys.exit(_self_test())
