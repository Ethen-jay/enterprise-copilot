# -*- coding: utf-8 -*-
"""
run_rag_eval.py —— RAG 端到端验收脚本（15 题 = 10 有答案 + 5 无依据，只读，每题只问一次）

流程：解析 eval/questions.md 表格 → 逐题直接调 rag_engine.chat()
      （检索 → 重排 → LLM → 反思校验，不走 HTTP，user="default"）
      → 有答案题核对“预期关键词”是否全部命中 + reflection.passed
      → 无依据题核对 reflection.no_answer 或 answer 出现“无法回答”
      → 存 JSON + 打印简表。

在项目根目录运行：
    python -B skills/rag_qa/eval/run_rag_eval.py
结果明细：skills/rag_qa/eval/runs/run_<时间戳>.json（最新一次另存 runs/latest.json）
"""

import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

# 把项目根加入模块搜索路径（脚本在 skills/rag_qa/eval/ 子目录，三级到 monorepo 根）
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from skills.rag_qa import rag_engine  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parents[0]   # skills/rag_qa/eval
QUESTIONS_MD = EVAL_DIR / "questions.md"
RUNS_DIR = EVAL_DIR / "runs"

# 拒答判定：answer 中出现这些字样之一即视为系统主动拒答（与 reflection.no_answer 等价兜底）
NO_ANSWER_MARKERS = ("无法回答", "未提及", "没有提到", "未涉及", "文档中没有", "文档中未")


# ----------------------------------------------------------------------
# 1. 解析 questions.md 表格
#    | R01 | 有答案/无依据 | 问题 | 预期关键词 |
# ----------------------------------------------------------------------
def parse_questions() -> list:
    cases = []
    for line in QUESTIONS_MD.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("| R"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        rid, category, question, expected = cells[0], cells[1], cells[2], cells[3]
        # 无依据题：expected 文本里写的是“（无依据，系统应拒答）”，无实际关键词
        if "无依据" in category or "无依据" in expected:
            kind = "no_evidence"
            points = []
        else:
            kind = "has_answer"
            # 必须命中的要点用中文分号分隔；一个要点内的等价写法用“或”分隔。
            points = [p.strip() for p in re.split(r"[；;]", expected) if p.strip()]
        cases.append({
            "id": rid, "category": category, "kind": kind,
            "question": question, "expected_points": points,
        })
    return cases


# ----------------------------------------------------------------------
# 2. 预期核对（有答案题）
# ----------------------------------------------------------------------
def _hit_point(point: str, answer_lower: str) -> bool:
    """核对单个要点：‘或’ 分隔的候选写法任一命中即可（子串匹配，已转小写）。"""
    for cand in point.split("或"):
        cand = cand.strip().lower()
        if cand and cand in answer_lower:
            return True
    return False


def check_answerable(points: list, answer: str) -> list:
    """有答案题：每个要点都必须命中。返回 [{point, hit}]。"""
    answer_lower = answer.lower()
    results = []
    for p in points:
        results.append({"point": p, "hit": _hit_point(p, answer_lower)})
    return results


def is_no_answer(answer: str, reflection: dict) -> bool:
    """无依据题：系统拒答即视为通过。
    判定 = reflection.no_answer == True，或 answer 文本里出现拒答字样。
    """
    if reflection.get("no_answer") is True:
        return True
    answer_lower = answer.lower()
    return any(m in answer_lower for m in NO_ANSWER_MARKERS)


# ----------------------------------------------------------------------
# 3. 逐题跑完整 RAG 链路
# ----------------------------------------------------------------------
async def run_one(case: dict) -> dict:
    question = case["question"]
    record = {
        "id": case["id"],
        "category": case["category"],
        "kind": case["kind"],
        "question": question,
        "expected_points": case["expected_points"],
        "answer": "",
        "citations": [],
        "reflection": {},
        "checks": [],
        "passed": False,
        "error": None,
        "elapsed_sec": 0,
    }
    t0 = time.time()
    try:
        result = await rag_engine.chat(question, history=None, user="default")
        answer = result.get("answer", "") or ""
        reflection = result.get("reflection", {}) or {}
        citations = result.get("citations", []) or []
        record["answer"] = answer
        record["reflection"] = reflection
        record["citations"] = citations

        if case["kind"] == "has_answer":
            checks = check_answerable(case["expected_points"], answer)
            record["checks"] = checks
            all_hit = all(c["hit"] for c in checks)
            # 有答案题：关键词全中 且 反思通过
            record["passed"] = bool(all_hit and reflection.get("passed") is True)
        else:
            # 无依据题：系统拒答即通过
            refused = is_no_answer(answer, reflection)
            record["checks"] = [{"point": "系统拒答", "hit": refused}]
            record["passed"] = bool(refused)
    except Exception as e:  # 兜底：任何意外都记录而不中断整批评测
        record["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        record["elapsed_sec"] = round(time.time() - t0, 1)
    return record


async def run_all(cases: list) -> list:
    records = []
    for case in cases:
        records.append(await run_one(case))
    return records


# ----------------------------------------------------------------------
# 4. main
# ----------------------------------------------------------------------
def _print_one(i: int, total: int, rec: dict) -> None:
    if rec["kind"] == "has_answer":
        if rec["passed"]:
            tag, detail = "PASS", "关键词全中"
        elif rec["error"]:
            tag, detail = "FAIL", "[异常] %s" % rec["error"][:80]
        else:
            miss = [c["point"] for c in rec["checks"] if not c["hit"]]
            reflection_passed = rec["reflection"].get("passed")
            extra = "" if reflection_passed else "；reflection.passed=%s" % reflection_passed
            tag = "FAIL"
            detail = "缺关键词: %s%s" % ("、".join(miss), extra)
    else:  # no_evidence
        if rec["passed"]:
            tag, detail = "PASS", "已拒答"
        elif rec["error"]:
            tag, detail = "FAIL", "[异常] %s" % rec["error"][:80]
        else:
            tag = "FAIL"
            detail = "未拒答(可能幻觉): %s" % rec["answer"][:80]
    print("[%2d/%d] %s %s  %.1fs  %s"
          % (i, total, rec["id"], tag, rec["elapsed_sec"], detail))


def main() -> int:
    cases = parse_questions()
    if not cases:
        print("未从 questions.md 解析到题目")
        return 1

    n_ans = sum(1 for c in cases if c["kind"] == "has_answer")
    n_no = sum(1 for c in cases if c["kind"] == "no_evidence")
    print("共 %d 题（有答案 %d + 无依据 %d），直接调 rag_engine.chat()，逐题端到端验收...\n"
          % (len(cases), n_ans, n_no))

    records = asyncio.run(run_all(cases))
    for i, rec in enumerate(records, 1):
        _print_one(i, len(records), rec)

    total = len(records)
    passed = sum(1 for r in records if r["passed"])
    ans_recs = [r for r in records if r["kind"] == "has_answer"]
    no_recs = [r for r in records if r["kind"] == "no_evidence"]
    ans_pass = sum(1 for r in ans_recs if r["passed"])
    no_pass = sum(1 for r in no_recs if r["passed"])

    print("\n" + "=" * 60)
    print("总通过率：%d/%d = %.1f%%  （目标≥80%%即≥12题）"
          % (passed, total, passed / total * 100))
    print("有答案题：%d/%d = %.1f%%" % (ans_pass, len(ans_recs),
                                       ans_pass / len(ans_recs) * 100 if ans_recs else 0))
    print("无依据题：%d/%d = %.1f%%" % (no_pass, len(no_recs),
                                       no_pass / len(no_recs) * 100 if no_recs else 0))
    print("=" * 60)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = {
        "run_time": stamp,
        "engine": "skills.rag_qa.rag_engine.chat",
        "user": "default",
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4),
        "answerable": {"total": len(ans_recs), "passed": ans_pass,
                       "pass_rate": round(ans_pass / len(ans_recs), 4) if ans_recs else 0},
        "no_evidence": {"total": len(no_recs), "passed": no_pass,
                        "pass_rate": round(no_pass / len(no_recs), 4) if no_recs else 0},
        "records": records,
    }
    payload = json.dumps(out, ensure_ascii=False, indent=2)

    # 默认写 eval/runs/；若目录无写权限回退到系统临时目录，保证明细不丢
    out_path = RUNS_DIR / ("run_%s.json" % stamp)
    latest_path = RUNS_DIR / "latest.json"
    fallback_used = False
    try:
        RUNS_DIR.mkdir(exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        latest_path.write_text(payload, encoding="utf-8")
    except PermissionError:
        fallback_used = True
        tmp_dir = Path(tempfile.gettempdir()) / "rag_eval_runs"
        tmp_dir.mkdir(exist_ok=True)
        out_path = tmp_dir / ("run_%s.json" % stamp)
        latest_path = tmp_dir / "latest.json"
        out_path.write_text(payload, encoding="utf-8")
        latest_path.write_text(payload, encoding="utf-8")
    print("明细已保存：%s%s" % (out_path, "（eval/runs 无写权限，已回退到临时目录）" if fallback_used else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
