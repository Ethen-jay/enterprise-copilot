# -*- coding: utf-8 -*-
"""智能文档转换 Agent 端到端测试：3 个验收用例（依赖 LLM API）。"""

from pathlib import Path

from skills.doc_agent import engine

ROOT = Path(__file__).resolve().parents[1]


def test_pdf_to_markdown():
    """用例①：PDF → Markdown（含表格，表格要被还原）。"""
    pdf_bytes = (ROOT / "data/tmp/test_source.pdf").read_bytes()
    result = engine.convert(pdf_bytes, "test_source.pdf", "把这个 PDF 转成 Markdown，保留表格")

    assert result["status"] == "ok", result.get("error")
    assert result["validation"]["passed"] is True

    md_text = Path(result["output_path"]).read_text(encoding="utf-8")
    table_lines = [l for l in md_text.splitlines() if l.strip().startswith("|")]
    assert len(table_lines) >= 3  # 表头 + 分隔 + 至少一行数据
    assert "张三" in md_text


def test_markdown_to_docx():
    """用例②：Markdown → DOCX。"""
    md_bytes = (ROOT / "data/tmp/test_source.md").read_bytes()
    result = engine.convert(md_bytes, "test_source.md", "转成 Word 文档")

    assert result["status"] == "ok", result.get("error")

    docx_path = Path(result["output_path"])
    assert docx_path.stat().st_size > 1000

    from docx import Document
    doc = Document(str(docx_path))
    assert len(doc.tables) >= 1


def test_docx_to_markdown():
    """用例③：DOCX → Markdown。"""
    docx_bytes = (ROOT / "data/tmp/test_source.docx").read_bytes()
    result = engine.convert(docx_bytes, "test_source.docx", "转成 Markdown")

    assert result["status"] == "ok", result.get("error")

    md_text = Path(result["output_path"]).read_text(encoding="utf-8")
    table_lines = [l for l in md_text.splitlines() if l.strip().startswith("|")]
    assert len(table_lines) >= 3
    assert "李四" in md_text