# -*- coding: utf-8 -*-
"""upload_classify.py —— 上传文件自动分流判别（可解释）

前端只保留「知识问答 / 数据问答」两个页签，上传入口不再让用户先选“转 Markdown”
还是“建表”。这里按**文件内容**判别更合适的去向，并把依据逐条返回：

    kind   = structured（结构化数据表）/ document（叙述性文档）/ database（SQLite）/ unknown
    route  = data（导入数据表）/ kb（入库知识库）/ database（接入数据源）/ ""（需用户选择）
    reasons= 人类可读的判定依据，例如“检测到 3 个工作表、每列有表头”

原则：
  - 宁可判 medium/low 让用户确认，也不在不确定时静默按默认处理；
  - 判别只看内容量与列结构，不调用大模型（快速、可复现）；
  - 前端拿到 route 后仍必须展示依据并允许一键改判。
"""

import csv
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import List, Optional

SQLITE_MAGIC = b"SQLite format 3\x00"

_KIND_ROUTE = {
    "structured": "data",
    "document": "kb",
    "database": "database",
    "unknown": "",
}


def _result(filename: str, kind: str, confidence: str, reasons: List[str],
            details: Optional[dict] = None) -> dict:
    return {
        "filename": filename,
        "kind": kind,
        "confidence": confidence,
        "route": _KIND_ROUTE.get(kind, ""),
        "reasons": reasons,
        "details": details or {},
    }


def _cell_text(value) -> str:
    return "" if value is None else str(value).strip()


# ----------------------------------------------------------------------
# SQLite
# ----------------------------------------------------------------------
def _classify_sqlite(path: Path, filename: str) -> dict:
    try:
        with path.open("rb") as fh:
            if fh.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                return _result(filename, "unknown", "low",
                               ["文件扩展名像数据库，但文件头不是 SQLite 格式"])
        con = sqlite3.connect("file:%s?mode=ro" % path.resolve().as_posix(), uri=True)
        try:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error) as e:
        return _result(filename, "unknown", "low", ["SQLite 文件无法打开：%s" % e])
    if not rows:
        return _result(filename, "unknown", "low", ["SQLite 文件中没有可用的表"])
    names = [r[0] for r in rows[:8]]
    return _result(
        filename, "database", "high",
        ["SQLite 数据库：检测到 %d 张表（%s%s）" % (
            len(rows), "、".join(names), " 等" if len(rows) > 8 else "")],
        {"tables": [r[0] for r in rows]},
    )


# ----------------------------------------------------------------------
# Excel
# ----------------------------------------------------------------------
def _classify_xlsx(path: Path, filename: str) -> dict:
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 - 损坏文件统一提示
        return _result(filename, "unknown", "low", ["Excel 解析失败，文件可能已损坏：%s" % e])
    sheets = []
    try:
        for ws in wb.worksheets:
            rows = 0
            max_cols = 0
            header: List[str] = []
            for idx, row in enumerate(ws.iter_rows(values_only=True)):
                if idx >= 30:
                    break
                values = [_cell_text(c) for c in row]
                non_empty = [v for v in values if v]
                if non_empty:
                    rows += 1
                    max_cols = max(max_cols, len(non_empty))
                    if not header:
                        header = non_empty[:6]
            sheets.append({
                "name": ws.title or "Sheet",
                "rows": rows,
                "columns": max_cols,
                "header": header,
            })
    finally:
        wb.close()

    rich = [s for s in sheets if s["columns"] >= 2 and s["rows"] >= 3]
    if rich:
        first = rich[0]
        return _result(
            filename, "structured", "high",
            [
                "检测到 %d 个工作表；工作表「%s」首行为表头（%s），共 %d 列、前 30 行中 %d 行为有效数据"
                % (len(sheets), first["name"], "、".join(first["header"][:4]) or "多列",
                   first["columns"], first["rows"] - 1),
                "多列且逐行有值，符合结构化数据表特征",
            ],
            {"sheets": sheets},
        )
    multi = [s for s in sheets if s["columns"] >= 2]
    if multi:
        first = multi[0]
        return _result(
            filename, "structured", "medium",
            ["工作表「%s」有 %d 列，但有效行数只有 %d 行（含表头）"
             % (first["name"], first["columns"], first["rows"]),
             "列结构清晰但数据量偏少，建议确认为数据表"],
            {"sheets": sheets},
        )
    return _result(
        filename, "document", "medium",
        ["工作表为单列文本或空表（列数 %d），未形成多列数据表" % (
            max([s["columns"] for s in sheets] or [0])),
         "更接近文档内容，建议按文档入库"],
        {"sheets": sheets},
    )


# ----------------------------------------------------------------------
# CSV / 文本
# ----------------------------------------------------------------------
def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _classify_csv(path: Path, filename: str) -> dict:
    text = _decode_text(path.read_bytes())
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(_cell_text(c) for c in r)]
    if not rows:
        return _result(filename, "unknown", "low", ["CSV 内容为空"])
    cols = max(len(r) for r in rows[:30])
    if cols >= 2 and len(rows) >= 2:
        header = [_cell_text(c) for c in rows[0][:5] if _cell_text(c)]
        return _result(
            filename, "structured", "high",
            ["CSV 检测到 %d 列、%d 行有效数据" % (cols, len(rows) - 1),
             "首行为表头（%s），符合结构化数据表特征" % ("、".join(header) or "多列")],
            {"columns": cols, "rows": len(rows) - 1, "header": header},
        )
    return _result(
        filename, "document", "medium",
        ["CSV 只有 %d 列，未形成多列数据表" % cols, "更接近文档内容，建议按文档入库"],
        {"columns": cols, "rows": len(rows)},
    )


def _table_line_stats(text: str) -> tuple:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    md_table = [ln for ln in lines if ln.count("|") >= 2]
    tsv = [ln for ln in lines if ln.count("\t") >= 1]
    return len(lines), len(md_table), len(tsv)


def _classify_text(path: Path, filename: str, ext: str) -> dict:
    text = _decode_text(path.read_bytes()[:262144])
    total, md_table, tsv = _table_line_stats(text)
    if ext in (".md", ".markdown", ".txt", ".log", ".tsv"):
        if md_table >= 3 and md_table >= total * 0.3:
            return _result(
                filename, "structured", "medium",
                ["文本中检测到 %d 行 Markdown 表格（占总行数 %d%%）"
                 % (md_table, int(md_table * 100 / max(total, 1))),
                 "存在连续成列的结构，可能是数据表"],
                {"lines": total, "table_lines": md_table},
            )
        if tsv >= 3 and tsv >= total * 0.5:
            return _result(
                filename, "structured", "medium",
                ["文本中 %d/%d 行含制表符分列" % (tsv, total), "可能是制表符分隔的数据表"],
                {"lines": total, "tsv_lines": tsv},
            )
        return _result(
            filename, "document", "high",
            ["纯文本 %d 行中未发现成列的表结构" % total, "按文档入库"],
            {"lines": total},
        )
    if ext in (".html", ".htm"):
        tables = len(re.findall(r"<table\b", text, re.I))
        if tables:
            return _result(
                filename, "structured", "medium",
                ["HTML 中检测到 %d 个 <table> 表格" % tables, "可能包含结构化数据"],
                {"tables": tables},
            )
        return _result(filename, "document", "high", ["HTML 文档，未检测到 <table> 表格"],
                       {"tables": 0})
    if ext == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return _result(filename, "document", "medium", ["JSON 解析失败，按文档处理"])
        if isinstance(data, list) and len(data) >= 2 and all(isinstance(x, dict) for x in data[:5]):
            keys = list(data[0].keys())
            if len(keys) >= 2:
                return _result(
                    filename, "structured", "medium",
                    ["JSON 数组含 %d 条对象记录，字段 %d 个（%s）"
                     % (len(data), len(keys), "、".join(keys[:4])),
                     "对象字段整齐，可按数据表建表"],
                    {"records": len(data), "fields": keys},
                )
        return _result(filename, "document", "medium", ["JSON 不是规整的对象数组，按文档处理"])
    return _result(filename, "document", "medium", ["文本内容未发现明显表结构，按文档处理"])


# ----------------------------------------------------------------------
# Word / PDF
# ----------------------------------------------------------------------
def _classify_docx(path: Path, filename: str) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return _result(filename, "unknown", "low", ["Word 文件解析失败，文件可能已损坏"])
    tables = len(re.findall(r"<w:tbl[ >]", xml))
    paragraphs = len(re.findall(r"<w:p[ >]", xml))
    text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))
    text = re.sub(r"\s+", "", text)
    if tables and paragraphs <= 15 and len(text) < 2000:
        return _result(
            filename, "structured", "medium",
            ["Word 文档含 %d 个表格，正文段落仅 %d 段、文字 %d 字" % (tables, paragraphs, len(text)),
             "内容以表格为主，可能是数据表"],
            {"tables": tables, "paragraphs": paragraphs, "chars": len(text)},
        )
    return _result(
        filename, "document", "high",
        ["Word 文档：正文 %d 段、表格 %d 个、文字约 %d 字" % (paragraphs, tables, len(text)),
         "以叙述性内容为主，判定为文档"],
        {"tables": tables, "paragraphs": paragraphs, "chars": len(text)},
    )


def _classify_pdf(path: Path, filename: str) -> dict:
    try:
        with path.open("rb") as fh:
            head = fh.read(5)
    except OSError:
        head = b""
    if head != b"%PDF-":
        return _result(filename, "unknown", "low", ["文件不是有效的 PDF（文件头校验失败）"])
    return _result(filename, "document", "high",
                   ["PDF 版式文档，以叙述性内容为主", "按文档入库"])


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def classify_file(path: Path, filename: str) -> dict:
    """按扩展名 + 文件内容判别去向；任何异常都退化为 unknown 让用户选择。"""
    ext = Path(filename).suffix.lower()
    try:
        if ext in (".db", ".sqlite", ".sqlite3"):
            return _classify_sqlite(path, filename)
        if ext in (".xlsx", ".xlsm"):
            return _classify_xlsx(path, filename)
        if ext == ".csv":
            return _classify_csv(path, filename)
        if ext == ".docx":
            return _classify_docx(path, filename)
        if ext == ".pdf":
            return _classify_pdf(path, filename)
        if ext in (".md", ".markdown", ".txt", ".log", ".tsv", ".json", ".html", ".htm"):
            return _classify_text(path, filename, ext)
        return _result(
            filename, "unknown", "low",
            ["无法识别的文件类型：%s" % (ext or "无扩展名"), "请手动选择存入知识库或导入数据表"],
        )
    except Exception as e:  # noqa: BLE001 - 判别失败不能阻断上传
        return _result(filename, "unknown", "low", ["判别过程出错：%s" % e])
