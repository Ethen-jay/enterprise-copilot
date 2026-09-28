# -*- coding: utf-8 -*-
"""
check_few_shot.py —— few_shot.md 范例 SQL 强校验（阶段 B）

为什么需要：few_shot 会被拼进 LLM 生成 prompt，任何一条范例 SQL 报错或口径写错，
都会被模型模仿。本脚本做两件事：
  1. 解析 semantic/few_shot.md 中每个 ### FSxx 分节及其 ```sql 代码块；
  2. 用只读 URI 在 data/ic_substrate.db 上逐条真实执行，打印返回行数/首行预览；
  3. 校验“- 涉及表”里的表名都真实存在。
任何一条执行失败 -> 打印错误并以非 0 退出，防止错误范例静默进入 prompt。

用法：python scripts/check_few_shot.py
"""
import re
import sqlite3
import sys
from pathlib import Path

# F1 迁移后：scripts 在 skills/text2sql/scripts 下
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
FEW_SHOT = PROJECT_ROOT / "semantic" / "few_shot.md"
DB_PATH = REPO_ROOT / "data" / "db" / "ic_substrate.db"

# 匹配一个 ### FSxx 分节（直到下一个 ### 或文件尾）
SECTION_RE = re.compile(r"(###\s+FS\d+\..*?)(?=\n###\s+FS|\Z)", re.S)


def parse_sections(md_text: str):
    """返回 [(fs编号, 标题, 涉及表列表, sql), ...]"""
    sections = []
    for m in SECTION_RE.finditer(md_text):
        block = m.group(1)
        title = block.splitlines()[0].strip("# ").strip()
        fs_id = re.match(r"FS\d+", title).group(0)

        tables = []
        tm = re.search(r"-\s*涉及表:\s*(.+)", block)
        if tm:
            tables = [t.strip() for t in tm.group(1).split(",") if t.strip()]

        sqls = re.findall(r"```sql\s*(.*?)```", block, re.S)
        if len(sqls) != 1:
            sections.append((fs_id, title, tables, None))  # 缺/多 SQL 块，后面报错
        else:
            sections.append((fs_id, title, tables, sqls[0].strip()))
    return sections


def main():
    if not FEW_SHOT.exists():
        print("[失败] 找不到 %s" % FEW_SHOT)
        sys.exit(2)

    md_text = FEW_SHOT.read_text(encoding="utf-8")
    sections = parse_sections(md_text)

    print("[1/2] 解析 few_shot.md：发现 %d 个 FS 分节" % len(sections))
    if len(sections) < 20:
        print("[失败] 范例不足 20 条（实际 %d 条）" % len(sections))
        sys.exit(1)

    # 只读连接（物理禁写，双保险）
    uri = "file:%s?mode=ro" % DB_PATH.resolve().as_posix()
    con = sqlite3.connect(uri, uri=True)
    real_tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    print("[2/2] 只读库逐条执行校验 ...\n")
    failures = []
    for fs_id, title, tables, sql in sections:
        # 表名校验
        bad_tables = [t for t in tables if t not in real_tables]
        if bad_tables:
            failures.append("%s 涉及不存在的表: %s" % (fs_id, bad_tables))
            print("  ✗ %s %s -> 涉及表不存在 %s" % (fs_id, title, bad_tables))
            continue
        if not sql:
            failures.append("%s 缺少唯一的 ```sql 代码块" % fs_id)
            print("  ✗ %s %s -> 缺少 SQL 代码块" % (fs_id, title))
            continue
        try:
            cur = con.execute(sql)
            rows = cur.fetchmany(3)
            n_cols = len(cur.description) if cur.description else 0
            preview = ""
            if rows:
                preview = " | 首行: " + ", ".join(str(v) for v in rows[0])[:80]
            print("  ✓ %s 执行成功（返回列%d, 取样%d行）%s" % (fs_id, n_cols, len(rows), preview))
        except Exception as e:
            failures.append("%s SQL 执行报错: %s" % (fs_id, e))
            print("  ✗ %s %s" % (fs_id, title))
            print("      报错: %s" % str(e).replace("\n", " "))

    con.close()

    print("")
    if failures:
        print("[校验失败] %d 条范例有问题：" % len(failures))
        for f in failures:
            print("  - " + f)
        sys.exit(1)
    print("[成功] %d 条范例全部在只读库执行通过。" % len(sections))


if __name__ == "__main__":
    main()
