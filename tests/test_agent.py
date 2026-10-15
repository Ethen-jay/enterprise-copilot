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


def test_disguised_legacy_doc():
    """用例④：旧版 .doc 改名成 .docx（骗过扩展名识别）。

    应按文件头魔数（OLE2）识别为旧版二进制格式并给出可读中文提示，
    而不是让 python-docx 抛出含内部临时路径的裸库报错。
    """
    # 旧版 Office 二进制格式（OLE2 复合文档）文件头，伪装成 .docx
    legacy_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 2048
    result = engine.convert(legacy_bytes, "old_report.docx", "转成 Markdown")

    assert result["status"] == "error"
    assert "旧版 Office 二进制格式" in result["error"]
    assert "Package not found" not in result["error"]