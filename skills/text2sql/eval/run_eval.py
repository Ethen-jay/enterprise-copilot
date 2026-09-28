# -*- coding: utf-8 -*-
"""
run_eval.py —— 阶段 E：20 题端到端验收脚本（只读，只调一次模型，不改问法重试）

流程：解析 eval/questions.md 表格 → 逐题走完整链路
      schema_linker → llm_client → sql_guard → executor
      → 把返回的所有单元格与“预期关键值”自动核对 → 存 JSON + 打印简表。

在项目根目录运行：
    python -B eval/run_eval.py
结果明细：eval/runs/run_<时间戳>.json（最新一次另存 eval/runs/latest.json）
"""

import json
import re
import sys
import tempfile
import time
from pathlib import Path

# 把项目根加入模块搜索路径（脚本在 eval/ 子目录）
PROJECT_ROOT = Path(__file__).resolve().parents[3]  # monorepo 根（eval 在 skills/text2sql/eval 下三级）
sys.path.insert(0, str(PROJECT_ROOT))

from skills.text2sql.schema_linker import build_context          # noqa: E402
from skills.text2sql.llm_client import generate_sql, LLMError    # noqa: E402
from skills.text2sql.sql_guard import check_sql                  # noqa: E402
from skills.text2sql.executor import execute_select, ReadonlyExecError  # noqa: E402
from skills.text2sql import config                               # noqa: E402

EVAL_DIR = Path(__file__).resolve().parents[0]   # skills/text2sql/eval
QUESTIONS_MD = EVAL_DIR / "questions.md"
RUNS_DIR = EVAL_DIR / "runs"

# 数字容差：相对误差 1%，且对小于 1 的比例再给 0.002 的绝对容差
REL_TOL = 0.01
ABS_TOL_SMALL = 0.002
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


# ----------------------------------------------------------------------
# 1. 解析 questions.md 表格
#    | E01 | 模块 | 问题 | 预期关键值 |
# ----------------------------------------------------------------------
def parse_questions() -> list:
    cases = []
    for line in QUESTIONS_MD.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("| E"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        eid, module, question, expected = cells[0], cells[1], cells[2], cells[3]
        # 必须命中的要点用中文分号分隔；一个要点内的等价写法用“或”分隔
        points = [p.strip() for p in re.split(r"[；;]", expected) if p.strip()]
        cases.append({
            "id": eid, "module": module, "question": question,
            "expected_points": points,
        })
    return cases


# ----------------------------------------------------------------------
# 2. 预期核对
# ----------------------------------------------------------------------
def _all_numbers(values: list) -> list:
    """把结果所有单元格摊平成字符串，再提取其中出现的全部数字。"""
    nums = []
    for v in values:
        nums.extend(float(x) for x in NUMBER_RE.findall(str(v)))
    return nums


def _hit_point(point: str, flat_text: str, numbers: list) -> dict:
    """
    核对一个要点：'或' 分隔的候选写法任一命中即可。
    候选是数字时按容差匹配；否则按子串匹配。
    返回 {point, hit, matched}。
    """
    candidates = [c.strip() for c in point.split("或") if c.strip()]
    for cand in candidates:
        as_num = NUMBER_RE.fullmatch(cand)
        if as_num:
            target = float(cand)
            for n in numbers:
                tol = max(abs(target) * REL_TOL, ABS_TOL_SMALL if abs(target) < 1 else 0.0,
                          0.5 if target == 0 else 0.0)
                if abs(n - target) <= tol:
                    return {"point": point, "hit": True, "matched": n}
        elif cand in flat_text:
            return {"point": point, "hit": True, "matched": cand}
    return {"point": point, "hit": False, "matched": None}


def check_expected(points: list, columns: list, rows: list) -> list:
    flat_values = []
    flat_values.extend(columns)
    for r in rows:
        flat_values.extend(r)
    flat_text = " ".join(str(v) for v in flat_values)
    numbers = _all_numbers(flat_values)
    return [_hit_point(p, flat_text, numbers) for p in points]


# ----------------------------------------------------------------------
# 3. 逐题跑完整链路
# ----------------------------------------------------------------------
def run_one(case: dict) -> dict:
    question = case["question"]
    record = {
        "id": case["id"], "module": case["module"], "question": question,
        "expected_points": case["expected_points"],
        "picked_tables": [], "sql": "", "columns": [], "rows": [],
        "checks": [], "passed": False, "stage": "", "error": None,
        "elapsed_sec": 0,
    }
    t0 = time.time()
    try:
        link = build_context(question)
        record["picked_tables"] = link.table_names

        llm = generate_sql(question, link.context_text,
                           snapshot_date=config.SNAPSHOT_DATE)
        record["model"] = llm.model
        if llm.refusal:
            record["stage"] = "llm_refusal"
            record["error"] = llm.refusal
            return record
        record["sql"] = llm.sql

        guarded = check_sql(llm.sql)
        if not guarded.ok:
            record["stage"] = "guard_reject"
            record["error"] = guarded.reason
            return record
        record["sql"] = guarded.sql

        result = execute_select(guarded.sql)
        record["columns"] = result["columns"]
        record["rows"] = result["rows"]
        record["stage"] = "executed"

        record["checks"] = check_expected(case["expected_points"],
                                          result["columns"], result["rows"])
        record["passed"] = all(c["hit"] for c in record["checks"])
    except (LLMError, ReadonlyExecError) as e:
        record["stage"] = "exception"
        record["error"] = str(e)
    except Exception as e:  # 兜底：任何意外都记录而不中断整批评测
        record["stage"] = "exception"
        record["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        record["elapsed_sec"] = round(time.time() - t0, 1)
    return record


def main() -> int:
    cases = parse_questions()
    if not cases:
        print("未从 questions.md 解析到题目")
        return 1
    print("共 %d 题，模型快照日=%s，开始逐题端到端验收（每题只问一次）...\n"
          % (len(cases), config.SNAPSHOT_DATE))

    records = []
    for i, case in enumerate(cases, 1):
        rec = run_one(case)
        records.append(rec)
        if rec["passed"]:
            tag = "PASS"
            detail = "命中 %d/%d" % (len(rec["checks"]), len(rec["checks"]))
        elif rec["error"]:
            tag = "FAIL"
            detail = "[%s] %s" % (rec["stage"], rec["error"][:80])
        else:
            miss = [c["point"] for c in rec["checks"] if not c["hit"]]
            tag = "FAIL"
            detail = "缺要点: %s" % "、".join(miss)
        print("[%2d/%d] %s %s  表=%s  %.1fs  %s"
              % (i, len(cases), rec["id"], tag, ",".join(rec["picked_tables"]),
                 rec["elapsed_sec"], detail))

    passed = sum(1 for r in records if r["passed"])
    total = len(records)
    print("\n" + "=" * 60)
    print("一次通过率：%d/%d = %.1f%%  （目标≥80%%即≥16题）"
          % (passed, total, passed / total * 100))
    print("=" * 60)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = {
        "run_time": stamp,
        "model": config.get_llm_settings().model,
        "snapshot_date": config.SNAPSHOT_DATE,
        "total": total, "passed": passed,
        "pass_rate": round(passed / total, 4),
        "records": records,
    }
    payload = json.dumps(out, ensure_ascii=False, indent=2)

    # 默认写 eval/runs/；若目录无写权限（受限沙箱）回退到系统临时目录，保证明细不丢
    out_path = RUNS_DIR / ("run_%s.json" % stamp)
    latest_path = RUNS_DIR / "latest.json"
    fallback_used = False
    try:
        RUNS_DIR.mkdir(exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        latest_path.write_text(payload, encoding="utf-8")
    except PermissionError:
        fallback_used = True
        tmp_dir = Path(tempfile.gettempdir()) / "erp_eval_runs"
        tmp_dir.mkdir(exist_ok=True)
        out_path = tmp_dir / ("run_%s.json" % stamp)
        latest_path = tmp_dir / "latest.json"
        out_path.write_text(payload, encoding="utf-8")
        latest_path.write_text(payload, encoding="utf-8")
    print("明细已保存：%s%s" % (out_path, "（eval/runs 无写权限，已回退到临时目录）" if fallback_used else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
