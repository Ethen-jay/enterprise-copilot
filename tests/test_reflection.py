# -*- coding: utf-8 -*-
"""测试 reflection 模块（纯本地，不依赖 LLM / API）。"""

from pathlib import Path

from skills.doc_agent.reflection import compare_metrics, reflect_conversion

ROOT = Path(__file__).resolve().parents[1]


def test_compare_metrics_pass_when_small_diff():
    src = {"paragraph_count": 20, "table_row_count": 5, "keywords": ["a", "b", "c"]}
    out = {"paragraph_count": 19, "table_row_count": 5, "keywords": ["a", "b", "c"]}
    assert compare_metrics(src, out)["passed"] is True


def test_compare_metrics_fail_when_table_lost():
    src = {"paragraph_count": 20, "table_row_count": 5, "keywords": ["a", "b", "c"]}
    out = {"paragraph_count": 20, "table_row_count": 0, "keywords": ["a", "b", "c"]}
    assert compare_metrics(src, out)["passed"] is False


def test_compare_metrics_fail_when_keywords_lost():
    src = {"paragraph_count": 10, "table_row_count": 3, "keywords": ["a", "b", "c", "d", "e"]}
    out = {"paragraph_count": 10, "table_row_count": 3, "keywords": ["a"]}
    assert compare_metrics(src, out)["passed"] is False


def test_compare_metrics_pass_within_tolerance():
    src = {"paragraph_count": 100, "table_row_count": 10, "keywords": ["a", "b", "c", "d"]}
    out = {"paragraph_count": 90, "table_row_count": 9, "keywords": ["a", "b", "c", "d"]}
    assert compare_metrics(src, out)["passed"] is True


def test_reflect_conversion_returns_structure():
    """PDF→MD 反思校验应返回完整结构（passed/cause/issues/details）。"""
    r = reflect_conversion(
        str(ROOT / "data/tmp/test_source.pdf"),
        str(ROOT / "data/tmp/test_source.md"),
        "pdf",
        "markdown",
    )
    assert isinstance(r, dict)
    for key in ("passed", "cause", "issues", "details"):
        assert key in r