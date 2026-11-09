# -*- coding: utf-8 -*-
"""
run_eval.py —— 统一评测入口（RAG / Text2SQL / 文档转换），支持 governed / naive / 消融

用法（项目根目录）：
    python scripts/run_eval.py --suite rag                       # governed + naive
    python scripts/run_eval.py --suite sql                       # governed + naive
    python scripts/run_eval.py --suite doc
    python scripts/run_eval.py --suite rag --variants governed,naive,ablate_hybrid_parent,ablate_rerank
    python scripts/run_eval.py --suite sql --variants governed,naive,ablate_metric_dict
    可选：--limit N（冒烟）；--out-dir 指定输出目录

运行模式：
  governed（治理后）：
    RAG = 文档治理（标题层级/页眉页脚/版本合并）+ 父子分块 + 向量+BM25 混合
          + 重排 + 0.3 阈值拒答 + 蕴含裁判；
    SQL = schema linking + metrics.md + few-shot + 人工指标口径字典。
  naive（平台开箱即用基线，真实走代码路径）：
    RAG = 原始文档直接固定长度切块（400 字/重叠 50，受 embedding 模型 512 token 上限约束；
          不重建结构、不清页眉页脚、不合并版本）+ 仅向量检索 + 不重排 + 不做阈值拒答
          + 不做蕴含裁判；
    SQL = 只给 tables.md（全表 DDL）+ 通用 system prompt，不注入口径字典/few-shot/
          metrics.md。
  消融（ablation，只关一项）：
    ablate_doc_governance = naive RAG（关闭文档治理）
    ablate_hybrid_parent  = 关闭父子分块 + 关闭 BM25 混合（保留重排/阈值/裁判）
    ablate_rerank         = 关闭重排（保留其余链路；无重排分数时阈值拒答自然失效）
    ablate_metric_dict    = 关闭人工指标口径字典（保留 schema/metrics/few-shot）

指标定义（写死，不允许手填）：
  RAG
    - Recall@K        ：期望文档（任一）出现在重排/检索后 top-K 的比例（文档级）
    - 答案正确率      ：应答题中，未被拒答且全部关键点命中的比例（规则判定）
    - 引用命中率      ：应答题中，引用来源包含期望文档（任一）的比例
    - 拒答准确率      ：(应拒答且拒答 + 应回答且未拒答) / 全部题
  Text2SQL
    - 执行成功率      ：应答题中，守卫通过且只读执行成功的比例
    - 结果集一致率    ：模型结果集与 gold_sql 结果集逐行比对一致的比例（不比 SQL 字符串）
    - 澄清/拒答命中   ：边界题中，clarify / refusal 行为符合预期的比例
    - 关键点命中率    ：应答题中，结果单元格覆盖全部 expected_points 的比例（辅助指标）
  文档转换
    - 通过率          ：转换成功 + validator 通过 + 关键词保留率 ≥ min_retention
    - 偏差分布        ：content_deviation = 1 - 关键词保留率（0 / ≤5% / ≤20% / >20%）
  公共
    - P50/P95 延迟    ：单题端到端耗时（秒）的分位数

判定方式：全部由脚本规则判定，不调用模型打分，不存在人工填写分数。
数字比对：结果集相对误差 1e-3；关键点相对误差 1%（绝对值 <1 时容差 0.5）。
"""

import argparse
import asyncio
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

EVAL_DIR = PROJECT_ROOT / "eval"
DATASET_DIR = EVAL_DIR / "datasets"
DEFAULT_OUT_DIR = EVAL_DIR / "runs"

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

METRICS = {
    "rag": {
        "Recall@K": "期望文档（任一）出现在重排/检索后 top-K 的比例（文档级召回）",
        "答案正确率": "应答题中，未被拒答且全部关键点命中的比例（规则判定）",
        "引用命中率": "应答题中，引用来源包含期望文档（任一）的比例",
        "拒答准确率": "(应拒答且拒答 + 应回答且未拒答) / 全部题",
        "P50/P95": "单题端到端耗时（秒）的 P50 / P95",
    },
    "sql": {
        "执行成功率": "应答题中，守卫通过且只读执行成功的比例",
        "结果集一致率": "模型结果集与 gold_sql 结果集逐行比对一致的比例（不比 SQL 字符串）",
        "澄清/拒答命中": "边界题中 clarify / refusal 行为符合预期的比例",
        "关键点命中率": "应答题中，结果单元格覆盖全部 expected_points 的比例（辅助指标）",
        "口径注明命中率": "带 note_points 的题中，回答的口径说明命中要求说法的比例（规则判定）",
        "P50/P95": "单题端到端耗时（秒）的 P50 / P95",
    },
    "doc": {
        "通过率": "转换成功 + validator 通过 + 关键词保留率 ≥ min_retention 的比例",
        "偏差分布": "content_deviation = 1 - 关键词保留率，按 0 / ≤5% / ≤20% / >20% 分桶",
    },
}

RAG_VARIANTS = {
    "governed": {"label": "治理后（完整链路）", "naive": False, "ablation": None},
    "naive": {"label": "naive 基线（无治理）", "naive": True, "ablation": None},
    "ablate_doc_governance": {"label": "消融：关闭文档治理", "naive": True, "ablation": None},
    "ablate_hybrid_parent": {
        "label": "消融：关闭父子+混合检索",
        "naive": False,
        "ablation": {"hybrid": False, "parent_child": False},
    },
    "ablate_rerank": {
        "label": "消融：关闭重排",
        "naive": False,
        "ablation": {"rerank": False},
    },
}

SQL_VARIANTS = {
    "governed": {"label": "治理后（完整链路）", "naive": False, "use_metric_dict": True},
    "naive": {"label": "naive 基线（仅 Schema）", "naive": True, "use_metric_dict": True},
    "ablate_metric_dict": {
        "label": "消融：关闭口径字典",
        "naive": False,
        "use_metric_dict": False,
    },
}


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------
def git_commit() -> str:
    try:
        sha = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return (sha or "unknown") + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def load_yaml(name: str) -> dict:
    with open(DATASET_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def norm_text(value) -> str:
    s = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"\s+", "", s).lower()


def norm_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("¥", "").rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def num_close(a: float, b: float, rel: float = 1e-3, abs_tol: float = 1e-6) -> bool:
    return abs(a - b) <= max(abs(b) * rel, abs_tol)


def point_hit(point: str, text: str) -> bool:
    flat = norm_text(text)
    numbers = [float(x) for x in NUMBER_RE.findall(text)]
    for cand in str(point).split("或"):
        cand = cand.strip()
        if not cand:
            continue
        if NUMBER_RE.fullmatch(cand):
            target = float(cand)
            for n in numbers:
                if num_close(n, target, rel=0.01, abs_tol=0.5 if abs(target) < 1 else 1e-6):
                    return True
        elif norm_text(cand) in flat:
            return True
    return False


def row_cells(row) -> list:
    return list(row) if isinstance(row, (list, tuple)) else [row]


def cell_equal(a, b) -> bool:
    na, nb = norm_number(a), norm_number(b)
    if na is not None and nb is not None:
        return num_close(na, nb)
    return norm_text(a) == norm_text(b)


def row_contains(model_row, gold_row) -> bool:
    cells = row_cells(model_row)
    return all(any(cell_equal(g, m) for m in cells) for g in row_cells(gold_row))


def rows_match(model_rows, gold_rows, mode: str) -> bool:
    if not gold_rows:
        return not model_rows
    if not model_rows:
        return False
    if mode == "first_row":
        return row_contains(model_rows[0], gold_rows[0])
    if len(model_rows) != len(gold_rows):
        return False
    remaining = list(model_rows)
    for g in gold_rows:
        for i, m in enumerate(remaining):
            if row_contains(m, g):
                remaining.pop(i)
                break
        else:
            return False
    return True


def run_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def pctl(values: list, p: float):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    idx = (len(ordered) - 1) * p
    lo, hi = int(idx), min(int(idx) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo), 2)


def mean(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


def write_outputs(suite: str, payload: dict, markdown: str, out_dir: Path) -> tuple:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = payload["header"]["run_stamp"]
    json_path = out_dir / f"eval_{suite}_{stamp}.json"
    md_path = out_dir / f"eval_{suite}_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    (out_dir / f"latest_{suite}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"latest_{suite}.md").write_text(markdown, encoding="utf-8")
    return json_path, md_path


def base_header(suite: str, sample_count: int) -> dict:
    import platform

    from common.config import get_llm_settings

    settings = get_llm_settings()
    return {
        "suite": suite,
        "git_commit": git_commit(),
        "model": settings.model,
        "temperature": settings.temperature,
        "rag_generation_temperature": 0.1,
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "run_stamp": run_ts(),
        "sample_count": sample_count,
        "project_root": str(PROJECT_ROOT),
        "judge": "规则判定（关键点/结果集比对），不使用模型打分",
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
    }


def header_md(header: dict, duration: float) -> str:
    lines = ["| 项目 | 值 |", "| --- | --- |"]
    lines.append(f"| 套件 | {header['suite']} |")
    lines.append(f"| git commit | `{header['git_commit']}` |")
    lines.append(f"| 模型 | {header['model']} |")
    lines.append(f"| 温度 | {header['temperature']}（RAG 生成侧 {header['rag_generation_temperature']}） |")
    lines.append(f"| 运行时间 | {header['run_at']} |")
    lines.append(f"| 总耗时 | {duration:.1f} 秒 |")
    lines.append(f"| 样本数 | {header['sample_count']} |")
    lines.append(f"| 判定方式 | {header['judge']} |")
    lines.append(f"| 运行环境 | Python {header['python']} / {header['platform']} |")
    lines.append("")
    return "\n".join(lines)


def metrics_md(suite: str) -> str:
    lines = ["## 指标定义", "", "| 指标 | 定义 |", "| --- | --- |"]
    for k, v in METRICS[suite].items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    return "\n".join(lines)


def _file_match(expected: str, actual: str) -> bool:
    if not actual:
        return False
    return Path(str(actual)).name == expected or str(actual).endswith(expected)


# ----------------------------------------------------------------------
# naive RAG 索引（固定长度切块 + 纯向量，真实调用 embedding 与 LLM）
# ----------------------------------------------------------------------
class NaiveIndex:
    """平台开箱即用基线：不治理文档，固定长度切块，仅向量检索。"""

    RAW_DOCS = [
        ("中文测试文档.md", PROJECT_ROOT / "中文测试文档.md"),
        ("benchmark_test.md", PROJECT_ROOT / "data" / "tmp" / "benchmark_test.md"),
        ("output_docx_to_markdown.md", PROJECT_ROOT / "data" / "output" / "output_docx_to_markdown.md"),
    ]

    def __init__(self, chunk_size: int = 400, overlap: int = 50):
        from langchain_core.documents import Document
        from skills.rag_qa.embeddings import APIEmbeddings

        self.chunk_size = chunk_size
        self.overlap = overlap
        self.docs = []
        self.missing = []
        for name, path in self.RAW_DOCS:
            if not path.exists():
                self.missing.append(name)
                continue
            raw = path.read_text(encoding="utf-8", errors="ignore")
            for idx, start in enumerate(range(0, max(len(raw), 1), chunk_size - overlap)):
                text = raw[start:start + chunk_size]
                if not text.strip():
                    continue
                self.docs.append(Document(
                    page_content=text,
                    metadata={"source": name, "chunk_index": idx, "chunking": "fixed-400"},
                ))
        self.embeddings = APIEmbeddings()
        self.vectors = self._embed([d.page_content for d in self.docs]) if self.docs else []

    def _embed(self, texts: list):
        import numpy as np

        return np.array(self.embeddings.embed_documents(texts), dtype="float32")

    def search(self, query: str, k: int = 3):
        import numpy as np

        if not self.docs:
            return []
        qv = np.array(self.embeddings.embed_query(query), dtype="float32")
        scores = self.vectors @ qv
        order = scores.argsort()[::-1][:k]
        results = []
        for i in order:
            doc = self.docs[int(i)]
            doc.metadata["vector_score"] = float(scores[int(i)])
            results.append(doc)
        return results


_NAIVE_CACHE = {}


def get_naive_index() -> NaiveIndex:
    if "idx" not in _NAIVE_CACHE:
        _NAIVE_CACHE["idx"] = NaiveIndex()
    return _NAIVE_CACHE["idx"]


def _looks_refused(text: str) -> bool:
    flat = norm_text(text)
    return ("无法回答" in flat) or ("根据现有文档无法回答" in flat) or ("无法从现有文档" in flat)


# ----------------------------------------------------------------------
# RAG
# ----------------------------------------------------------------------
async def _rag_one(case: dict, user: str, variant: dict, top_k: int) -> dict:
    from skills.rag_qa import rag_engine

    observer: dict = {}
    started = time.time()
    rec = {
        "id": case["id"], "question": case["question"],
        "should_refuse": bool(case.get("should_refuse")),
    }
    try:
        if variant["naive"]:
            from skills.rag_qa import llm_client as rag_llm

            idx = get_naive_index()
            docs = idx.search(case["question"], k=top_k)
            answer = await rag_llm.chat(case["question"], docs)
            retrieved_files = [d.metadata.get("source", "") for d in docs]
            cited_files = list(retrieved_files)
            refused = _looks_refused(answer)
            rec.update({
                "retrieved_files": retrieved_files,
                "cited_files": cited_files,
                "refused": refused,
                "answer": answer,
                "attempts": 1,
            })
        else:
            result = await rag_engine.chat(
                case["question"], user=user, observer=observer,
                ablation=variant.get("ablation"),
            )
            answer = result.get("answer", "")
            citations = result.get("citations", [])
            retrieved_files = [r.get("source", "") for r in observer.get("retrieved", [])]
            cited_files = [c.get("file", "") for c in citations]
            refused = bool(observer.get("refused")) or _looks_refused(answer)
            rec.update({
                "answer": answer,
                "refused": refused,
                "refusal_reason": observer.get("refusal_reason"),
                "retrieved_files": retrieved_files,
                "cited_files": cited_files,
                "attempts": observer.get("attempts", 1),
            })

        expected = case.get("expected_sources") or []
        rec["recall_hit"] = (not expected) or any(
            _file_match(e, f) for e in expected for f in retrieved_files
        )
        rec["citation_hit"] = (not expected) or any(
            _file_match(e, f) for e in expected for f in cited_files
        )
        checks = [{"point": p, "hit": point_hit(p, answer)} for p in (case.get("key_points") or [])]
        rec["checks"] = checks
        rec["answer_correct"] = (not rec["should_refuse"]) and (not rec["refused"]) and all(
            c["hit"] for c in checks
        )
        if case.get("should_refuse"):
            rec["status"] = "PASS" if rec["refused"] else "FAIL"
        else:
            rec["status"] = "PASS" if rec["answer_correct"] else "FAIL"
    except Exception as e:  # noqa: BLE001
        rec.update({"status": "ERROR", "error": f"{type(e).__name__}: {e}"})
    rec["elapsed_sec"] = round(time.time() - started, 2)
    return rec


def _rag_summary(records: list) -> dict:
    valid = [r for r in records if r.get("status") != "ERROR"]
    answerable = [r for r in valid if not r["should_refuse"]]
    refuse_cases = [r for r in valid if r["should_refuse"]]
    elapsed = [r["elapsed_sec"] for r in valid]
    correct_refuse = sum(1 for r in refuse_cases if r.get("refused"))
    correct_answer = sum(1 for r in answerable if not r.get("refused"))
    return {
        "样本数": len(records),
        "Recall@K": round(mean([1.0 if r.get("recall_hit") else 0.0 for r in answerable]), 4),
        "答案正确率": round(mean([1.0 if r.get("answer_correct") else 0.0 for r in answerable]), 4),
        "引用命中率": round(mean([1.0 if r.get("citation_hit") else 0.0 for r in answerable]), 4),
        "拒答准确率": round(
            (correct_refuse + correct_answer) / max(len(answerable) + len(refuse_cases), 1), 4
        ),
        "应答题数": len(answerable),
        "应拒答题数": len(refuse_cases),
        "错误数": len(records) - len(valid),
        "P50": pctl(elapsed, 0.50),
        "P95": pctl(elapsed, 0.95),
    }


async def run_rag(args, variants: list) -> tuple:
    ds = load_yaml("rag.yaml")
    cases = ds["cases"][: args.limit] if args.limit else ds["cases"]
    user = (ds.get("meta") or {}).get("user", "Ethen")
    top_k = (ds.get("meta") or {}).get("top_k", 3)

    results = {}
    total_started = time.time()
    for name in variants:
        variant = RAG_VARIANTS[name]
        print(f"\n--- RAG 变体：{name}（{variant['label']}）---")
        records, started = [], time.time()
        for i, case in enumerate(cases, 1):
            rec = await _rag_one(case, user, variant, top_k)
            records.append(rec)
            print(
                f"[{i}/{len(cases)}] {rec['id']} {rec['status']} "
                f"{'refused' if rec.get('refused') else ''} {rec['elapsed_sec']}s"
            )
        results[name] = {
            "label": variant["label"],
            "summary": _rag_summary(records),
            "duration_sec": round(time.time() - started, 1),
            "records": records,
        }

    duration = time.time() - total_started
    header = base_header("rag", len(cases))
    header.update({
        "dataset": "eval/datasets/rag.yaml",
        "user": user,
        "top_k": top_k,
        "variants": variants,
        "naive_corpus": [n for n, _ in NaiveIndex.RAW_DOCS],
    })
    payload = {
        "header": header,
        "metrics": METRICS["rag"],
        "variants": results,
        "duration_sec": round(duration, 1),
    }

    md = [f"# RAG 评测报告（{header['run_stamp']}）", "", header_md(header, duration)]
    md += _rag_variant_note(variants)
    md.append(metrics_md("rag"))
    md.append("## 对比")
    md.append("")
    md.append("| 变体 | 样本数 | Recall@K | 答案正确率 | 引用命中率 | 拒答准确率 | P50(s) | P95(s) |")
    md.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for name in variants:
        s = results[name]["summary"]
        md.append(
            f"| {name} | {s['样本数']} | {s['Recall@K']} | {s['答案正确率']} | "
            f"{s['引用命中率']} | {s['拒答准确率']} | {s['P50']} | {s['P95']} |"
        )
    md.append("")
    for name in variants:
        s = results[name]["summary"]
        md.append(f"## 明细：{name}")
        md.append("")
        md.append(f"应答题 {s['应答题数']} / 应拒答 {s['应拒答题数']} / 错误 {s['错误数']}")
        md.append("")
        md.append("| 用例 | 问题 | 结果 | 召回 | 引用 | 命中要点 | 拒答 | 耗时 |")
        md.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in results[name]["records"]:
            checks = r.get("checks") or []
            hit = sum(1 for c in checks if c["hit"])
            md.append(
                f"| {r['id']} | {r['question'][:28]} | {r['status']} | "
                f"{'是' if r.get('recall_hit') else '否'} | "
                f"{'是' if r.get('citation_hit') else '否'} | "
                f"{hit}/{len(checks)} | {'是' if r.get('refused') else '否'} | "
                f"{r['elapsed_sec']}s |"
            )
        md.append("")
    return payload, "\n".join(md), duration


def _rag_variant_note(variants: list) -> list:
    lines = ["## 运行模式说明", ""]
    for name in variants:
        v = RAG_VARIANTS[name]
        if v["naive"]:
            lines.append(f"- `{name}`：原始文档固定长度切块（400 字/重叠 50，受 embedding 模型 512 token 上限约束）+ 仅向量检索 + 不重排 + 无阈值拒答 + 无蕴含裁判。")
        elif v["ablation"]:
            lines.append(f"- `{name}`：关闭 {v['ablation']}，其余链路不变。")
        else:
            lines.append("- `governed`：文档治理 + 父子分块 + 向量/BM25 混合 + 重排 + 0.3 阈值 + 蕴含裁判。")
    lines.append("")
    return lines


# ----------------------------------------------------------------------
# Text2SQL
# ----------------------------------------------------------------------
def _naive_sql_context() -> str:
    path = PROJECT_ROOT / "skills" / "text2sql" / "semantic" / "tables.md"
    return (
        "# Schema 上下文（平台原始 DDL，无口径/范例）\n\n"
        + path.read_text(encoding="utf-8")
    )


def _run_sql_one(case: dict, variant: dict, anchor, snapshot, naive_context: str) -> dict:
    from skills.text2sql.schema_linker import build_context
    from skills.text2sql.llm_client import generate_sql, LLMError
    from skills.text2sql.sql_guard import check_sql
    from skills.text2sql.executor import execute_select, ReadonlyExecError

    started = time.time()
    rec = {
        "id": case["id"], "module": case.get("module", ""),
        "question": case["question"], "expect": case.get("expect", "answer"),
        "expected_points": case.get("expected_points") or [],
        "expected_tables": case.get("expected_tables") or [],
        "result_mode": case.get("result_mode", "exact"),
        "note_points": case.get("note_points") or [],
        "judge": case.get("judge", ""),
    }
    try:
        if variant["naive"]:
            context_text = naive_context
            rec["picked_tables"] = []
            rec["table_hit"] = False
        else:
            link = build_context(
                case["question"], anchor_date=anchor.date,
                use_metric_dict=variant["use_metric_dict"],
            )
            context_text = link.context_text
            rec["picked_tables"] = link.table_names
            rec["table_hit"] = (
                not rec["expected_tables"]
                or all(t in link.table_names for t in rec["expected_tables"])
            )

        llm = generate_sql(
            case["question"], context_text,
            anchor_date=anchor.date, snapshot_date=snapshot,
        )
        rec["model"] = getattr(llm, "model", None)
        rec["explanation"] = (getattr(llm, "explanation", "") or "").strip()
        if getattr(llm, "refusal", None):
            rec["stage"] = "refusal"
            rec["error"] = llm.refusal
        elif getattr(llm, "clarify", None):
            rec["stage"] = "clarify"
            rec["error"] = llm.clarify
        else:
            rec["sql"] = llm.sql
            guarded = check_sql(llm.sql)
            if not guarded.ok:
                rec["stage"] = "guard_reject"
                rec["error"] = guarded.reason
            else:
                rec["sql"] = guarded.sql
                result = execute_select(guarded.sql)
                rec["columns"] = result["columns"]
                rec["rows"] = result["rows"]
                rec["stage"] = "executed"
                if rec["expected_points"]:
                    rec["checks"] = [
                        {"point": p, "hit": _sql_point_hit(p, result["columns"], result["rows"])}
                        for p in rec["expected_points"]
                    ]
                if case.get("gold_sql"):
                    gold = execute_select(case["gold_sql"])
                    rec["gold_columns"] = gold["columns"]
                    rec["gold_rows"] = gold["rows"]
                    rec["result_match"] = rows_match(
                        rec["rows"], gold["rows"], rec["result_mode"]
                    )
    except (LLMError, ReadonlyExecError) as e:
        rec["stage"] = "exception"
        rec["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        rec["stage"] = "exception"
        rec["error"] = f"{type(e).__name__}: {e}"

    expect = rec["expect"]
    if expect == "answer":
        ok = bool(rec.get("result_match"))
    elif expect == "clarify":
        ok = rec.get("stage") == "clarify"
    elif expect == "clarify_or_answer":
        # 口径改写题：澄清 或 按替代维度答对，均算命中
        ok = rec.get("stage") == "clarify" or bool(rec.get("result_match"))
    else:
        ok = rec.get("stage") == "refusal"
    if ok and rec["note_points"] and rec.get("stage") == "executed":
        rec["note_checks"] = [
            {"point": p, "hit": point_hit(p, rec.get("explanation", ""))}
            for p in rec["note_points"]
        ]
        ok = all(c["hit"] for c in rec["note_checks"])
    rec["status"] = "PASS" if ok else "FAIL"
    rec["elapsed_sec"] = round(time.time() - started, 2)
    return rec


def _sql_summary(records: list) -> dict:
    answerable = [r for r in records if r["expect"] == "answer"]
    boundary = [r for r in records if r["expect"] != "answer"]
    executed = [r for r in answerable if r.get("stage") == "executed"]
    matched = [r for r in executed if r.get("result_match")]
    keypoint = [r for r in executed if r.get("checks") and all(c["hit"] for c in r["checks"])]
    noted = [r for r in records if r.get("note_points") and r.get("stage") == "executed"]
    elapsed = [r["elapsed_sec"] for r in records]
    return {
        "样本数": len(records),
        "执行成功率": round(len(executed) / len(answerable), 4) if answerable else 0.0,
        "结果集一致率": round(len(matched) / len(answerable), 4) if answerable else 0.0,
        "澄清/拒答命中": round(
            sum(1 for r in boundary if r["status"] == "PASS") / len(boundary), 4
        ) if boundary else None,
        "关键点命中率": round(len(keypoint) / len(answerable), 4) if answerable else 0.0,
        "口径注明命中率": round(
            sum(1 for r in noted if r.get("note_checks") and all(c["hit"] for c in r["note_checks"]))
            / len(noted), 4
        ) if noted else None,
        "应答题数": len(answerable),
        "边界题数": len(boundary),
        "表命中率": round(
            sum(1 for r in answerable if r.get("table_hit")) / len(answerable), 4
        ) if answerable else 0.0,
        "P50": pctl(elapsed, 0.50),
        "P95": pctl(elapsed, 0.95),
    }


def run_sql(args, variants: list) -> tuple:
    from skills.text2sql import config
    from skills.text2sql.anchor import get_anchor_date, get_snapshot_date

    ds = load_yaml("text2sql.yaml")
    cases = ds["cases"][: args.limit] if args.limit else ds["cases"]
    anchor = get_anchor_date()
    snapshot = get_snapshot_date()
    naive_context = _naive_sql_context()

    results = {}
    total_started = time.time()
    for name in variants:
        variant = SQL_VARIANTS[name]
        print(f"\n--- SQL 变体：{name}（{variant['label']}）---")
        records, started = [], time.time()
        for i, case in enumerate(cases, 1):
            rec = _run_sql_one(case, variant, anchor, snapshot, naive_context)
            records.append(rec)
            print(
                f"[{i}/{len(cases)}] {rec['id']} {rec['status']} "
                f"stage={rec.get('stage')} {rec['elapsed_sec']}s"
            )
        results[name] = {
            "label": variant["label"],
            "summary": _sql_summary(records),
            "duration_sec": round(time.time() - started, 1),
            "records": records,
        }

    duration = time.time() - total_started
    header = base_header("sql", len(cases))
    header.update({
        "dataset": "eval/datasets/text2sql.yaml",
        "anchor_date": anchor.date,
        "anchor_source": anchor.source,
        "snapshot_date": snapshot,
        "model": config.get_llm_settings().model,
        "variants": variants,
    })
    payload = {
        "header": header,
        "metrics": METRICS["sql"],
        "variants": results,
        "duration_sec": round(duration, 1),
    }

    md = [f"# Text2SQL 评测报告（{header['run_stamp']}）", "", header_md(header, duration)]
    md.append(f"业务基准日：{anchor.date}（{anchor.source}），账龄快照日：{snapshot}\n")
    md.append("## 运行模式说明")
    md.append("")
    for name in variants:
        v = SQL_VARIANTS[name]
        if v["naive"]:
            md.append(f"- `{name}`：仅 tables.md 全表 DDL + 通用 system prompt；不注入 metrics.md / few-shot / 口径字典 / schema linking。")
        elif not v["use_metric_dict"]:
            md.append(f"- `{name}`：保留 schema linking + metrics.md + few-shot，仅关闭人工指标口径字典。")
        else:
            md.append("- `governed`：schema linking + metrics.md + few-shot + 人工指标口径字典。")
    md.append("")
    md.append(metrics_md("sql"))
    md.append("## 对比")
    md.append("")
    md.append("| 变体 | 样本数 | 执行成功率 | 结果集一致率 | 澄清/拒答命中 | 关键点命中率 | 口径注明命中率 | P50(s) | P95(s) |")
    md.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name in variants:
        s = results[name]["summary"]
        md.append(
            f"| {name} | {s['样本数']} | {s['执行成功率']} | {s['结果集一致率']} | "
            f"{s['澄清/拒答命中']} | {s['关键点命中率']} | {s['口径注明命中率']} | {s['P50']} | {s['P95']} |"
        )
    md.append("")
    judged = [c for c in cases if c.get("judge")]
    if judged:
        md.append("## 口径题判定方式（新增部门口径题）")
        md.append("")
        md.append("| 用例 | 期望行为 | 判定方式 | 口径注明要求 |")
        md.append("| --- | --- | --- | --- |")
        for c in judged:
            notes = "；".join(c.get("note_points") or []) or "-"
            md.append(
                f"| {c['id']} | {c.get('expect', 'answer')} | {c['judge']} | {notes} |"
            )
        md.append("")
    for name in variants:
        s = results[name]["summary"]
        md.append(f"## 明细：{name}")
        md.append("")
        md.append(f"应答题 {s['应答题数']} / 边界题 {s['边界题数']} / schema 表命中率 {s['表命中率']}")
        md.append("")
        md.append("| 用例 | 问题 | 结果 | 阶段 | 结果集一致 | 口径注明 | 关键点 | 耗时 |")
        md.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in results[name]["records"]:
            checks = r.get("checks") or []
            kp = f"{sum(1 for c in checks if c['hit'])}/{len(checks)}" if checks else "-"
            note_checks = r.get("note_checks") or []
            note = f"{sum(1 for c in note_checks if c['hit'])}/{len(note_checks)}" if note_checks else "-"
            if r.get("result_match") is None:
                match = "-"
            else:
                match = "是" if r["result_match"] else "否"
            md.append(
                f"| {r['id']} | {r['question'][:26]} | {r['status']} | {r.get('stage', '-')} | "
                f"{match} | {note} | {kp} | {r['elapsed_sec']}s |"
            )
        md.append("")
    return payload, "\n".join(md), duration


def _sql_point_hit(point: str, columns: list, rows: list) -> bool:
    values = list(columns)
    for r in rows:
        values.extend(r)
    return point_hit(point, " ".join(str(v) for v in values))


# ----------------------------------------------------------------------
# 文档转换
# ----------------------------------------------------------------------
FIXTURE_MD = """# 转换保真基准 ALPHA-7731

BRAVO-2048 出现在正文段落中，用于校验段落是否保留。

## 参数表

| 项目 | 数值 |
| --- | --- |
| 铜厚 | 铜厚18微米 |
| 设备 | TS-900 |

CHARLIE-4096 是结尾标记。
"""


def _build_fixtures(base: Path) -> dict:
    from skills.doc_agent.converters import docx_converter, excel_converter, html_converter, text_converter

    fixtures = {}
    md_path = base / "fixture.md"
    md_path.write_text(FIXTURE_MD, encoding="utf-8")
    fixtures["markdown"] = md_path

    txt_path = base / "fixture.txt"
    txt_path.write_text(text_converter.markdown_to_text(FIXTURE_MD), encoding="utf-8")
    fixtures["txt"] = txt_path

    html_path = base / "fixture.html"
    html_path.write_text(html_converter.markdown_to_html(FIXTURE_MD), encoding="utf-8")
    fixtures["html"] = html_path

    docx_path = base / "fixture.docx"
    docx_converter.markdown_to_docx(FIXTURE_MD, output_path=str(docx_path))
    fixtures["docx"] = docx_path

    xlsx_path = base / "fixture.xlsx"
    excel_converter.markdown_to_excel(FIXTURE_MD, output_path=str(xlsx_path))
    fixtures["xlsx"] = xlsx_path

    pdf_path = base / "fixture.pdf"
    docx_converter.markdown_to_pdf(FIXTURE_MD, output_path=str(pdf_path))
    fixtures["pdf"] = pdf_path
    return fixtures


def _extract_text(path: Path, fmt: str) -> str:
    if fmt in ("markdown", "txt"):
        return path.read_text(encoding="utf-8", errors="ignore")
    if fmt == "html":
        from bs4 import BeautifulSoup

        return BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser").get_text(" ")
    if fmt == "docx":
        from docx import Document

        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.extend(cell.text for cell in row.cells)
        return "\n".join(parts)
    if fmt == "xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                parts.extend("" if v is None else str(v) for v in row)
        wb.close()
        return "\n".join(parts)
    if fmt == "pdf":
        import fitz

        with fitz.open(str(path)) as doc:
            return "\n".join(page.get_text() for page in doc)
    return ""


def _retention(text: str, keywords: list) -> float:
    flat = norm_text(text)
    if not keywords:
        return 1.0
    hit = sum(1 for k in keywords if norm_text(k) in flat)
    return hit / len(keywords)


def run_doc(args) -> tuple:
    ds = load_yaml("doc_convert.yaml")
    meta = ds.get("meta") or {}
    sources = meta.get("sources") or ["markdown", "txt", "html", "docx", "xlsx", "pdf"]
    targets = meta.get("targets") or sources
    keywords = meta.get("keywords") or []
    min_retention = float(meta.get("min_retention", 0.8))

    from skills.doc_agent.graph import _execute_conversion  # noqa: PLC2701
    from skills.doc_agent.planner_v2 import TaskPlan, create_plan
    from skills.doc_agent.validator import validate

    tmp = Path(tempfile.mkdtemp(prefix="doc_eval_"))
    fixtures = _build_fixtures(tmp)
    combos = [(s, t) for s in sources for t in targets]
    if args.limit:
        combos = combos[: args.limit]

    records = []
    t0 = time.time()
    for i, (src, tgt) in enumerate(combos, 1):
        started = time.time()
        rec = {"id": f"C{i:02d}", "source": src, "target": tgt}
        try:
            plan = TaskPlan(task_type="convert", source_format=src, target_format=tgt)
            out = _execute_conversion(str(fixtures[src]), plan, None)
            val = validate(out, src, tgt)
            text = _extract_text(Path(out), tgt)
            retention = _retention(text, keywords)
            rec.update({
                "output": out,
                "validation": val,
                "retention": round(retention, 4),
                "deviation": round(1 - retention, 4),
                "passed": bool(val.get("passed")) and retention >= min_retention,
                "status": "PASS" if (val.get("passed") and retention >= min_retention) else "FAIL",
            })
        except Exception as e:  # noqa: BLE001
            rec.update({
                "status": "FAIL", "stage": "exception",
                "error": f"{type(e).__name__}: {e}", "passed": False,
                "retention": 0.0, "deviation": 1.0,
            })
        finally:
            rec["elapsed_sec"] = round(time.time() - started, 2)
            records.append(rec)
        print(f"[{i}/{len(combos)}] {src}->{tgt} {rec['status']} dev={rec.get('deviation')}")

    edges = []
    for case in ds.get("edge_cases") or []:
        started = time.time()
        rec = {"id": case["id"], "name": case.get("name", ""), "expect": case["expect"]}
        try:
            fixture_map = {
                "empty_md": ("empty.md", b""),
                "legacy_docx": ("legacy.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512),
                "unsupported_bin": ("blob.bin", b"\x00\x01\x02\x03" * 64),
                "csv_supported": ("data.csv", "项目,数值\n设备,TS-900\n".encode("utf-8")),
                "unicode_name": ("中文 空格 文档.md", FIXTURE_MD.encode("utf-8")),
            }
            name, content = fixture_map[case["fixture"]]
            src_path = tmp / name
            src_path.write_bytes(content)
            plan = create_plan(case["instruction"], name)
            out = _execute_conversion(str(src_path), plan, None)
            val = validate(out, plan.source_format, plan.target_format)
            ok = bool(val.get("passed"))
            rec.update({"output": out, "validation": val, "stage": "converted"})
            rec["status"] = "PASS" if (ok if case["expect"] == "ok" else not ok) else "FAIL"
            if rec["status"] == "FAIL":
                rec["note"] = "未报错，已按文本兜底转换（与期望的显式报错不符）"
        except Exception as e:  # noqa: BLE001
            rec.update({"stage": "exception", "error": f"{type(e).__name__}: {e}"})
            rec["status"] = "PASS" if case["expect"] == "error" else "FAIL"
        finally:
            rec["elapsed_sec"] = round(time.time() - started, 2)
            edges.append(rec)
        print(f"[edge] {rec['id']} {rec['status']}")

    duration = time.time() - t0
    passed = sum(1 for r in records if r["passed"])
    deviations = [r["deviation"] for r in records]
    buckets = {
        "0": sum(1 for d in deviations if d == 0),
        "≤5%": sum(1 for d in deviations if 0 < d <= 0.05),
        "≤20%": sum(1 for d in deviations if 0.05 < d <= 0.2),
        ">20%": sum(1 for d in deviations if d > 0.2),
    }
    summary = {
        "通过率": round(passed / len(records), 4) if records else 0.0,
        "通过数": passed,
        "组合数": len(records),
        "偏差分布": buckets,
        "偏差均值": round(sum(deviations) / len(deviations), 4) if deviations else 0.0,
        "边界用例通过": f"{sum(1 for r in edges if r['status'] == 'PASS')}/{len(edges)}",
        "关键词数": len(keywords),
        "min_retention": min_retention,
    }

    header = base_header("doc", len(records) + len(edges))
    header["dataset"] = "eval/datasets/doc_convert.yaml"
    payload = {"header": header, "metrics": METRICS["doc"], "summary": summary,
               "records": records, "edge_cases": edges, "duration_sec": round(duration, 1)}

    md = [f"# 文档转换评测报告（{header['run_stamp']}）", "", header_md(header, duration)]
    md.append(metrics_md("doc"))
    md.append("## 汇总")
    md.append("")
    md.append("| 指标 | 值 |")
    md.append("| --- | --- |")
    md.append(f"| 通过率 | {summary['通过率']}（{passed}/{len(records)}） |")
    md.append(f"| 偏差均值 | {summary['偏差均值']} |")
    md.append(f"| 边界用例 | {summary['边界用例通过']} |")
    md.append("")
    md.append("| 偏差区间 | 数量 |")
    md.append("| --- | --- |")
    for k, v in buckets.items():
        md.append(f"| {k} | {v} |")
    md.append("")
    md.append("## 36 组合明细")
    md.append("")
    md.append("| 编号 | 源 → 目标 | 结果 | 保留率 | 偏差 | 说明 |")
    md.append("| --- | --- | --- | --- | --- | --- |")
    for r in records:
        note = r.get("error") or "、".join((r.get("validation") or {}).get("issues") or []) or ""
        md.append(
            f"| {r['id']} | {r['source']} → {r['target']} | {r['status']} | "
            f"{r.get('retention', 0)} | {r.get('deviation', 1)} | {note[:40]} |"
        )
    md.append("")
    md.append("## 边界样例")
    md.append("")
    md.append("| 编号 | 场景 | 期望 | 结果 | 说明 |")
    md.append("| --- | --- | --- | --- | --- |")
    for r in edges:
        note = r.get("note") or r.get("error") or "、".join((r.get("validation") or {}).get("issues") or []) or ""
        md.append(f"| {r['id']} | {r['name']} | {r['expect']} | {r['status']} | {note[:50]} |")
    md.append("")
    return payload, "\n".join(md), duration


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="统一评测：RAG / Text2SQL / 文档转换")
    parser.add_argument("--suite", choices=["rag", "sql", "doc", "all"], default="all")
    parser.add_argument("--variants", default="governed,naive",
                        help="逗号分隔：governed,naive,ablate_hybrid_parent,ablate_rerank,ablate_metric_dict")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（冒烟）")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args()
    out_dir = Path(args.out_dir)

    suites = ["rag", "sql", "doc"] if args.suite == "all" else [args.suite]
    for suite in suites:
        print(f"\n===== 开始评测：{suite} =====")
        if suite == "rag":
            variants = [v for v in args.variants.split(",") if v in RAG_VARIANTS]
            payload, md, duration = asyncio.run(run_rag(args, variants))
        elif suite == "sql":
            variants = [v for v in args.variants.split(",") if v in SQL_VARIANTS]
            payload, md, duration = run_sql(args, variants)
        else:
            payload, md, duration = run_doc(args)
        jp, mp = write_outputs(suite, payload, md, out_dir)
        if suite in ("rag", "sql"):
            print(f"完成 {suite}：")
            for name, item in payload["variants"].items():
                print(f"  {name}: {json.dumps(item['summary'], ensure_ascii=False)}")
        else:
            print(f"完成 {suite}：{json.dumps(payload['summary'], ensure_ascii=False)}")
        print(f"JSON: {jp}\nMarkdown: {mp}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
