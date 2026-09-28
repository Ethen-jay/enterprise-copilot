# -*- coding: utf-8 -*-
"""测试 content_parser 解析（纯本地，不依赖 LLM / API）。"""

from pathlib import Path

from skills.doc_agent.content_parser import parse_metrics

ROOT = Path(__file__).resolve().parents[1]


def test_parse_markdown():
    m = parse_metrics(str(ROOT / "中文测试文档.md"), "markdown")
    assert m["paragraph_count"] > 0
    assert len(m["keywords"]) > 0


def test_parse_pdf():
    m = parse_metrics(str(ROOT / "data/tmp/test_source.pdf"), "pdf")
    assert m["paragraph_count"] > 0
    assert m["table_row_count"] >= 1  # 该 PDF 含表格
    assert len(m["keywords"]) > 0


def test_parse_docx():
    m = parse_metrics(str(ROOT / "data/tmp/test_source.docx"), "docx")
    assert m["paragraph_count"] > 0
    assert m["table_row_count"] >= 1  # 该 DOCX 含表格