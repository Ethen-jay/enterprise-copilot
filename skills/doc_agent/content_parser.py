"""共用解析层：校验器和转换器共用的内容指标提取。

用 jieba 分词提取关键词，用 pdfplumber/python-docx 统一段落数和表格行数。
"""

from collections import Counter
from pathlib import Path
from typing import List

import jieba

# jieba 精简模式，关闭日志
jieba.setLogLevel(20)  # logging.WARNING

# 中文停用词
STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有",
    "看", "好", "自己", "这", "那", "与", "及", "或", "但", "而", "以", "对",
    "为", "从", "把", "被", "让", "使", "给", "向", "于", "中", "等", "之",
    "其", "此", "该", "这些", "那些", "什么", "怎么", "为什么", "哪", "哪些",
    "可以", "可能", "应该", "需要", "已经", "正在", "将", "会", "能", "做",
    "它们", "他们", "她", "他", "它", "我们", "你们", "她们", "这个", "那个",
    "地", "得", "吗", "呢", "吧", "啊", "哦", "嗯", "呀",
}


def extract_keywords(text: str, top_n: int = 10) -> List[str]:
    """jieba 分词 + 停用词过滤，取 top_n 高频词。

    过滤掉纯符号、Markdown 语法标记、纯数字。
    """
    if not text or not text.strip():
        return []
    words = jieba.lcut(text)
    # Markdown 语法标记
    md_tokens = {"##", "###", "####", "---", "------", "|", "---|", "***", "```", "-"}
    filtered = [
        w for w in words
        if len(w) >= 2
        and w not in STOP_WORDS
        and w not in md_tokens
        and not w.isspace()
        and not all(c in "#-*|>`~_=" for c in w)  # 过滤纯符号
        and not w.replace(".", "").replace("-", "").isdigit()  # 过滤纯数字/日期片段
    ]
    if not filtered:
        return []
    counter = Counter(filtered)
    return [w for w, _ in counter.most_common(top_n)]


def parse_metrics(file_path: str | Path, fmt: str) -> dict:
    """统一解析文件内容指标。

    参数:
        file_path: 文件路径。
        fmt: 文件格式（pdf/docx/markdown/html/xlsx）。

    返回:
        {"paragraph_count": int, "table_row_count": int, "keywords": list[str]}
    """
    path = Path(file_path)
    if not path.exists():
        return {"paragraph_count": 0, "table_row_count": 0, "keywords": []}

    if fmt == "pdf":
        return _parse_pdf(path)
    elif fmt == "docx":
        return _parse_docx(path)
    elif fmt in ("markdown", "md"):
        return _parse_markdown(path)
    elif fmt == "html":
        return _parse_html(path)
    elif fmt == "xlsx":
        return _parse_xlsx(path)
    elif fmt in ("txt", "text"):
        return _parse_text(path.read_text(encoding="utf-8"))
    else:
        # 尝试当文本读
        try:
            text = path.read_text(encoding="utf-8")
            return _parse_text(text)
        except Exception:
            return {"paragraph_count": 0, "table_row_count": 0, "keywords": []}


def _parse_pdf(path: Path) -> dict:
    """PDF: pdfplumber 提取段落数 + 表格行数。"""
    import pdfplumber
    para_count = 0
    table_row_count = 0
    all_text = []

    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = [l for l in text.splitlines() if l.strip()]
            para_count += len(lines)
            all_text.append(text)

            tables = page.extract_tables() or []
            for table in tables:
                table_row_count += len(table)

    full_text = "\n".join(all_text)
    return {
        "paragraph_count": para_count,
        "table_row_count": table_row_count,
        "keywords": extract_keywords(full_text),
    }


def _parse_docx(path: Path) -> dict:
    """DOCX: python-docx 提取段落数 + 表格行数。"""
    from docx import Document
    doc = Document(str(path))
    para_count = len([p for p in doc.paragraphs if p.text.strip()])
    table_row_count = 0
    all_text = []

    for table in doc.tables:
        table_row_count += len(table.rows)
        for row in table.rows:
            for cell in row.cells:
                all_text.append(cell.text)

    for p in doc.paragraphs:
        all_text.append(p.text)

    return {
        "paragraph_count": para_count,
        "table_row_count": table_row_count,
        "keywords": extract_keywords(" ".join(all_text)),
    }


def _parse_markdown(path: Path) -> dict:
    """Markdown: 正则统计段落 + Markdown 表格行。"""
    import re
    text = path.read_text(encoding="utf-8")
    # 段落：非空行（不含表格行）
    lines = text.splitlines()
    para_lines = [l for l in lines if l.strip() and not l.strip().startswith("|")]
    para_count = len(para_lines)
    # 表格行：以 | 开头的行（不含分隔线）
    table_lines = [l for l in lines if l.strip().startswith("|") and "---" not in l]
    table_row_count = len(table_lines)

    return {
        "paragraph_count": para_count,
        "table_row_count": table_row_count,
        "keywords": extract_keywords(text),
    }


def _parse_html(path: Path) -> dict:
    """HTML: 简单统计段落和表格行。"""
    text = path.read_text(encoding="utf-8")
    import re
    # 段落
    paras = re.findall(r"<p[^>]*>(.*?)</p>", text, re.DOTALL | re.IGNORECASE)
    para_count = len(paras)
    # 表格行
    rows = re.findall(r"<tr[^>]*>.*?</tr>", text, re.DOTALL | re.IGNORECASE)
    table_row_count = len(rows)

    # 去标签提取纯文本
    clean = re.sub(r"<[^>]+>", " ", text)
    return {
        "paragraph_count": para_count,
        "table_row_count": table_row_count,
        "keywords": extract_keywords(clean),
    }


def _parse_xlsx(path: Path) -> dict:
    """XLSX: openpyxl 统计行数。"""
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True)
    para_count = 0
    table_row_count = 0
    all_text = []

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            cells = [c for c in row if c.value is not None]
            if cells:
                table_row_count += 1
                for c in cells:
                    all_text.append(str(c.value))

    wb.close()
    return {
        "paragraph_count": table_row_count,  # Excel 里行就是段落的近似
        "table_row_count": table_row_count,
        "keywords": extract_keywords(" ".join(all_text)),
    }


def _parse_text(text: str) -> dict:
    """纯文本解析。"""
    lines = [l for l in text.splitlines() if l.strip()]
    return {
        "paragraph_count": len(lines),
        "table_row_count": 0,
        "keywords": extract_keywords(text),
    }
