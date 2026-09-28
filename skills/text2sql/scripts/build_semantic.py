# -*- coding: utf-8 -*-
"""
build_semantic.py —— 语义层知识库构建脚本（阶段 A）

作用：
    解析 data_model/schema_postgres.sql（PostgreSQL DDL，列名的唯一权威），
    自动生成 semantic/tables.md：22 张表的真实列名 / 类型 / 约束 / 注释 / 实测行数。
    同时只读连接 data/ic_substrate.db 做交叉校验，防止 DDL 与实际库悄悄漂移。

设计原则：
    1. 列信息一律脚本解析，禁止手抄（手抄必错）；
    2. 强校验：表数量不符 / DDL 列与 SQLite 实际列不一致 → 打印差异并以非 0 退出，不生成文件；
    3. 纯标准库实现，无需安装任何第三方依赖。

用法：
    python scripts/build_semantic.py      # 在项目根目录或任意目录执行均可
"""

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------
# 0. 路径定位：脚本位于 <项目根>/scripts/ 下，项目根取上一级即可，不依赖当前工作目录
# ----------------------------------------------------------------------
# F1 迁移后：scripts 在 skills/text2sql/scripts 下，parents[1] 为子系统包目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
DDL_PATH = PROJECT_ROOT / "data_model" / "schema_postgres.sql"
DB_PATH = REPO_ROOT / "data" / "db" / "ic_substrate.db"
OUT_PATH = PROJECT_ROOT / "semantic" / "tables.md"

# DDL 中“分组横幅”只认这三个（其他横幅如“常用索引”忽略）
SECTION_NAMES = {
    "主数据": "一、主数据表（dim_ 开头）",
    "ERP 事实": "二、ERP 事实表（销售/出货/库存/应收）",
    "MES 事实": "三、MES 事实表（工单/过站/品质/设备/客诉）",
}


# ----------------------------------------------------------------------
# 1. 解析 PostgreSQL DDL
# ----------------------------------------------------------------------
def parse_ddl(sql_text: str):
    """
    返回：
        sections: {分组名: [表名, ...]}（保持 DDL 中出现顺序）
        tables:   {表名: {"comment": str, "columns": [列字典, ...]}}
        fk_list:  [(从表.从列, 到表), ...]  外键关联，供生成“关联速查”
    """
    # 1.1 表注释：COMMENT ON TABLE xxx IS '...';
    table_comments = dict(
        re.findall(r"COMMENT\s+ON\s+TABLE\s+(\w+)\s+IS\s+'([^']*)'", sql_text, re.I)
    )

    # 1.2 单趟扫描：横幅决定“当前分组”，CREATE TABLE 出现时立即记下它属于哪个分组
    #     （不能先扫完横幅再解析建表块，否则所有表都会错误地落到最后一个分组）
    current_section = "主数据"
    sections = {name: [] for name in SECTION_NAMES}
    table_section = {}          # {表名: 分组名}
    tables = {}
    fk_list = []

    banner_re = re.compile(r"^\s*--\s*-{3,}\s*(.+?)\s*-{3,}\s*$")
    create_start_re = re.compile(r"CREATE\s+TABLE\s+(\w+)", re.I)
    create_re = re.compile(r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\)\s*;", re.I | re.S)

    for line in sql_text.splitlines():
        m = banner_re.match(line)
        if m:
            title = m.group(1).strip()
            if title in SECTION_NAMES:
                current_section = title
            continue
        m = create_start_re.search(line)
        if m:
            table_section[m.group(1)] = current_section

    # 1.3 抽取每张表的定义块并解析列
    for table_name, body in create_re.findall(sql_text):
        columns = []
        for raw_line in body.splitlines():
            # 先拆行内注释，再去掉结尾逗号
            if "--" in raw_line:
                code_part, comment = raw_line.split("--", 1)
                comment = comment.strip()
            else:
                code_part, comment = raw_line, ""
            code_part = code_part.strip().rstrip(",").strip()
            if not code_part:
                continue

            tokens = code_part.split()
            head = tokens[0].lower()
            # 跳过表级约束行（主键/唯一/外键/检查约束）
            if head in ("primary", "unique", "constraint", "foreign", "check"):
                continue

            col_name = tokens[0]
            col_type = tokens[1] if len(tokens) > 1 else ""

            # 约束标记：PK / NOT NULL / 外键指向
            flags = []
            code_upper = code_part.upper()
            if re.search(r"\bPRIMARY\s+KEY\b", code_upper):
                flags.append("PK")
            if re.search(r"\bNOT\s+NULL\b", code_upper):
                flags.append("NOT NULL")
            fk_match = re.search(r"REFERENCES\s+(\w+)\s*\(\s*(\w+)\s*\)", code_upper)
            if fk_match:
                # 表名在 DDL 中是小写，正则被 upper 后需从原文重新取
                fk_real = re.search(r"REFERENCES\s+(\w+)\s*\(\s*(\w+)\s*\)", code_part, re.I)
                ref_table, ref_col = fk_real.group(1), fk_real.group(2)
                flags.append("FK→%s" % ref_table)
                fk_list.append(("%s.%s" % (table_name, col_name),
                                "%s.%s" % (ref_table, ref_col)))

            columns.append({
                "name": col_name,
                "type": col_type,
                "flags": "、".join(flags),
                "comment": comment,
            })

        sec = table_section.get(table_name, "主数据")
        tables[table_name] = {
            "comment": table_comments.get(table_name, ""),
            "columns": columns,
            "section": sec,
        }
        sections[sec].append(table_name)

    return sections, tables, fk_list


# ----------------------------------------------------------------------
# 2. 只读连接 SQLite：取实际表/列/行数，并与 DDL 强校验
# ----------------------------------------------------------------------
def verify_with_sqlite(tables: dict):
    """返回 (行数字典, 差异列表)。只读 URI，物理上无法写入。"""
    uri = "file:%s?mode=ro" % DB_PATH.resolve().as_posix()
    con = sqlite3.connect(uri, uri=True)
    cur = con.cursor()

    diffs = []
    db_tables = {
        r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    ddl_tables = set(tables.keys())

    if db_tables != ddl_tables:
        diffs.append("表集合不一致：仅DDL有=%s；仅SQLite有=%s"
                     % (sorted(ddl_tables - db_tables), sorted(db_tables - ddl_tables)))

    row_counts = {}
    for t in sorted(ddl_tables & db_tables):
        # 列名一致性
        db_cols = [r[1] for r in cur.execute('PRAGMA table_info("%s")' % t)]
        ddl_cols = [c["name"] for c in tables[t]["columns"]]
        if db_cols != ddl_cols:
            diffs.append(
                "表 %s 列不一致：\n  DDL : %s\n  实际: %s" % (t, ddl_cols, db_cols)
            )
        row_counts[t] = cur.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        if not tables[t]["columns"]:
            diffs.append("表 %s 未解析出任何列（DDL 解析可能出错）" % t)

    con.close()
    return row_counts, diffs


# ----------------------------------------------------------------------
# 3. 生成 tables.md
# ----------------------------------------------------------------------
def render_markdown(sections, tables, fk_list, row_counts) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append("# 语义层 · 22 表结构说明书（自动生成，请勿手改）\n")
    lines.append("> - 列名/类型/注释来源：`data_model/schema_postgres.sql`（PostgreSQL DDL，列名唯一权威）")
    lines.append("> - 行数来源：`data/ic_substrate.db` 只读实测；重新生成：`python scripts/build_semantic.py`")
    lines.append("> - 生成时间：%s；数据时间窗：**2026-06-01 ~ 2026-09-24**" % now)
    lines.append(">")
    lines.append("> **方言提醒（写 SQL 必读）**：DDL 是 PostgreSQL 类型（VARCHAR/NUMERIC/SMALLINT/TIMESTAMP），")
    lines.append("> 但执行库是 **SQLite**。生成 SQL 必须用 SQLite 方言：日期处理用 `date()/datetime()/strftime()`，")
    lines.append("> 禁止使用 PostgreSQL 专属语法（`::` 类型转换、`INTERVAL`、`DATE_TRUNC`、`ILIKE` 等）。\n")

    table_seq = 0
    for sec_title, sec_heading in SECTION_NAMES.items():
        table_names = sections.get(sec_title, [])
        lines.append("\n## %s（%d 张）\n" % (sec_heading, len(table_names)))
        for t in table_names:
            table_seq += 1
            info = tables[t]
            row_n = row_counts.get(t, "?")
            heading = "### %d. %s" % (table_seq, t)
            if info["comment"]:
                heading += " —— %s" % info["comment"]
            lines.append(heading + "\n")
            lines.append("实测 **%s** 行。\n" % format(row_n, ","))
            lines.append("| 列名 | 类型 | 约束 | 注释 |")
            lines.append("|---|---|---|---|")
            for c in info["columns"]:
                comment = c["comment"].replace("|", "\\|")
                lines.append("| %s | %s | %s | %s |"
                             % (c["name"], c["type"], c["flags"], comment))
            lines.append("")

    # 外键关联速查（去重、按从表排序）
    lines.append("\n## 四、表间关联速查（外键，JOIN 时按这些列）\n")
    for (src, dst) in sorted(set(fk_list)):
        lines.append("- `%s` → `%s`" % (src, dst))
    lines.append("")
    lines.append("> 注意（非外键但必须按 ID 关联的列）：")
    lines.append("> - `fact_shipment.customer_id`、`fact_shipment_item.product_id`、")
    lines.append(">   `fact_work_order.product_id`、`fact_work_order.line_id`、")
    lines.append(">   `fact_process_record.lot_id / process_id / equipment_id`、")
    lines.append(">   `fact_inventory.material_id`、`fact_receivable.customer_id` 等在 DDL 中未声明外键，")
    lines.append(">   但语义上关联同名主数据/事实表，**按 ID 列关联，禁止在单号/名称上做字符串截切匹配**。\n")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 4. 主流程
# ----------------------------------------------------------------------
def main():
    if not DDL_PATH.exists():
        print("[失败] 找不到 DDL：%s" % DDL_PATH)
        sys.exit(2)
    if not DB_PATH.exists():
        print("[失败] 找不到 SQLite：%s" % DB_PATH)
        sys.exit(2)

    sql_text = DDL_PATH.read_text(encoding="utf-8")
    sections, tables, fk_list = parse_ddl(sql_text)

    print("[1/3] DDL 解析完成：%d 张表" % len(tables))
    for sec, names in sections.items():
        if names:
            print("      - %-8s %d 张：%s" % (sec, len(names), ", ".join(names)))

    print("[2/3] 与 SQLite 只读交叉校验 ...")
    row_counts, diffs = verify_with_sqlite(tables)
    total_rows = sum(row_counts.values())
    print("      实测总行数：%s（22 表）" % format(total_rows, ","))

    if diffs:
        print("\n[校验失败] DDL 与 SQLite 存在差异，已中止生成 tables.md：")
        for d in diffs:
            print("  ✗ " + d.replace("\n", "\n    "))
        sys.exit(1)
    print("      表数量、每表列名与 SQLite 完全一致 ✓")

    print("[3/3] 生成 %s ..." % OUT_PATH)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        render_markdown(sections, tables, fk_list, row_counts), encoding="utf-8"
    )
    print("      外键关联 %d 条已写入关联速查。" % len(set(fk_list)))
    print("\n[成功] semantic/tables.md 已生成，可进入下一步。")


if __name__ == "__main__":
    main()
