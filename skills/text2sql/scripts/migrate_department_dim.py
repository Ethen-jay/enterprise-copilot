# -*- coding: utf-8 -*-
"""给 fact_process_record 补 department（部门快照）列，幂等可重复执行。

口径：
    每条过站记录的 department = 该记录 operator_id 在 dim_employee.department 的取值，
    即「谁操作、归属谁的部门」，是记录时点快照；不按设备/产线/班次/工序名称反推。
    现有 31,991 条记录 operator_id 100% 可解析，回填后 0 NULL。
    若未来出现无法解析的记录，department 保留 NULL，按口径字典约定记为「未归属」并单列，
    不并入任何部门。

用法：
    python skills/text2sql/scripts/migrate_department_dim.py
    python skills/text2sql/scripts/migrate_department_dim.py --db data/db/ic_substrate.db

说明：
    - 业务库运行期由 executor 以只读模式（mode=ro + query_only）打开，本脚本只做离线结构升级；
    - 重复执行安全：列已存在则跳过 ALTER，只回填仍为 NULL 的行；
    - 退出码：0=回填完整；2=仍有无法解析的记录（需人工确认）。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = REPO_ROOT / "data" / "db" / "ic_substrate.db"

ADD_COLUMN_SQL = "ALTER TABLE fact_process_record ADD COLUMN department VARCHAR(20)"
BACKFILL_SQL = (
    "UPDATE fact_process_record SET department = ("
    "SELECT e.department FROM dim_employee e "
    "WHERE e.employee_id = fact_process_record.operator_id"
    ") WHERE department IS NULL"
)


def migrate(db_path: Path) -> int:
    if not db_path.exists():
        print("[error] 数据库不存在：%s" % db_path)
        return 1

    con = sqlite3.connect(str(db_path), timeout=60)
    con.execute("PRAGMA busy_timeout = 60000")
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(fact_process_record)")]
        if not cols:
            print("[error] 表 fact_process_record 不存在")
            return 1

        if "department" in cols:
            print("[ddl] department 已存在，跳过")
        else:
            con.execute(ADD_COLUMN_SQL)
            print("[ddl] ALTER TABLE fact_process_record ADD COLUMN department VARCHAR(20)")

        updated = con.execute(BACKFILL_SQL).rowcount
        con.commit()
        print("[dml] 回填行数：%d" % updated)

        total, nulls = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN department IS NULL THEN 1 ELSE 0 END) "
            "FROM fact_process_record"
        ).fetchone()
        print("[check] 总行数：%d，department 为空：%d，覆盖率：%.2f%%"
              % (total, nulls, (total - nulls) * 100.0 / total if total else 0.0))
        for dept, cnt in con.execute(
            "SELECT COALESCE(department, '未归属'), COUNT(*) FROM fact_process_record "
            "GROUP BY COALESCE(department, '未归属') ORDER BY COUNT(*) DESC"
        ):
            print("        %-6s %d" % (dept, cnt))
        return 0 if nulls == 0 else 2
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="fact_process_record 部门快照列迁移")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 业务库路径")
    args = ap.parse_args()
    return migrate(Path(args.db))


if __name__ == "__main__":
    sys.exit(main())
