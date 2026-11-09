# -*- coding: utf-8 -*-
"""
datasource.py —— 上传 xlsx/csv 直接问数

职责：
  1. 解析 xlsx/csv → 规范化表名/列名 → 推断类型 → 写入独立的用户库
     data/db/user_uploads.db（不污染只读业务库 ic_substrate.db）；
  2. 维护导入登记表 _meta_sources（源文件名、sheet、行数、列定义）；
  3. 为 schema_linker 提供动态表上下文（查询时用附加库 userdb 限定）。

重复上传策略：
  - 同名表默认拒绝（409），避免悄悄覆盖线上正在被问数的数据；
  - 显式 replace=true 时在同一事务里 DROP + CREATE + 全量重灌，失败整体回滚。
    这样“换一版 Excel 重新上传”是可预期操作，也不会留下新旧混合的半张表。

查询侧不改动既有安全语义：executor 以只读 URI ATTACH userdb，
sql_guard 的 SELECT 白名单 / 自动 LIMIT / 超时检查全部照旧生效。
"""

import csv
import io
import json
import logging
import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import MAX_DATASOURCE_ROWS, USER_DB_PATH

logger = logging.getLogger("text2sql.datasource")

# 同一时刻只允许一个导入写事务，避免并发 DDL 互相踩踏
_IMPORT_LOCK = threading.Lock()

REGISTRY_TABLE = "_meta_sources"
MAX_IDENT_LEN = 60

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_BAD_IDENT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_MULTI_UNDERSCORE_RE = re.compile(r"_+")


class DataSourceError(Exception):
    """导入失败；message 可直接展示，status_code 由接口层映射。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ----------------------------------------------------------------------
# 标识符 / 表头规范化
# ----------------------------------------------------------------------
def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def safe_identifier(raw: str, fallback: str, digit_prefix: str = "u_") -> str:
    """中文可读、内部安全的标识符：保留中英文/数字，其余替换为下划线。

    SQLite 对标识符大小写不敏感（ASCII 部分），这里统一保留原样以便
    表名/列名在提示词与结果里可读；查询语句由模型用双引号包裹。
    """
    s = _cell_text(raw)
    s = _BAD_IDENT_RE.sub("_", s)
    s = _MULTI_UNDERSCORE_RE.sub("_", s).strip("_")
    if not s:
        s = fallback
    if s[0].isdigit():
        s = digit_prefix + s
    return s[:MAX_IDENT_LEN]


def _unique_names(names: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    result: List[str] = []
    for name in names:
        key = name.lower()
        if key not in seen:
            seen[key] = 1
            result.append(name)
            continue
        seen[key] += 1
        result.append("%s_%d" % (name, seen[key]))
    return result


# ----------------------------------------------------------------------
# 解析：xlsx / csv
# ----------------------------------------------------------------------
def _read_xlsx_sheets(path: Path) -> List[Tuple[str, List[List[object]]]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise DataSourceError(f"Excel 解析失败：{path.name}（文件可能已损坏）") from e
    try:
        return [
            (ws.title or "Sheet", [list(row) for row in ws.iter_rows(values_only=True)])
            for ws in wb.worksheets
        ]
    finally:
        wb.close()


def _decode_csv(data: bytes) -> str:
    last_err: Optional[Exception] = None
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as e:
            last_err = e
    raise DataSourceError("CSV 编码无法识别（已尝试 UTF-8 / GB18030）") from last_err


def _read_csv_rows(path: Path) -> List[Tuple[str, List[List[object]]]]:
    text = _decode_csv(path.read_bytes())
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [list(row) for row in csv.reader(io.StringIO(text), dialect)]
    return [("csv", rows)]


# ----------------------------------------------------------------------
# 表结构推断
# ----------------------------------------------------------------------
def _infer_type(values: List[object]) -> str:
    non_empty = [_cell_text(v) for v in values if _cell_text(v)]
    if not non_empty:
        return "TEXT"
    is_int = all(_INT_RE.match(v) for v in non_empty)
    if is_int:
        return "INTEGER"
    if all(_FLOAT_RE.match(v) for v in non_empty):
        return "REAL"
    return "TEXT"


def _coerce(value: object, col_type: str) -> Tuple[object, bool]:
    """按推断类型转换；返回 (值, 是否仍符合该类型)。"""
    text = _cell_text(value)
    if text == "":
        return None, True
    if col_type == "INTEGER":
        if _INT_RE.match(text):
            return int(text), True
        return text, False
    if col_type == "REAL":
        if _FLOAT_RE.match(text):
            return float(text), True
        return text, False
    return text, True


def _prepare_sheet(
    rows: List[List[object]],
    fallback_name: str,
) -> Optional[dict]:
    """把原始网格整理成 {columns:[(name,type)], rows:[[...]], notes:[...]}。

    处理规则：
      - 去掉全空行/全空列；
      - 首个非空行若是“标题行”（仅 1 个非空单元格、下一行更像表头），跳过并记录；
      - 空表头补 col_N，重名表头加 _2/_3 后缀；
      - 每列按非空值推断 INTEGER/REAL/TEXT，混合类型降级 TEXT；
      - 全空列直接丢弃（不建列）。
    """
    grid = [[_cell_text(c) for c in row] for row in rows]
    grid = [row for row in grid if any(c for c in row)]
    if not grid:
        return None

    notes: List[str] = []
    # 标题行：只有 1 个非空单元格，且下一行至少 2 个非空单元格
    if len(grid) >= 2:
        first_count = sum(1 for c in grid[0] if c)
        second_count = sum(1 for c in grid[1] if c)
        if first_count <= 1 and second_count >= 2:
            title = next((c for c in grid[0] if c), "")
            notes.append(f"已跳过标题行：{title}" if title else "已跳过空标题行")
            grid = grid[1:]

    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]

    header = grid[0]
    data = grid[1:]

    # 丢弃全空列（表头和数据都为空的列）
    keep_idx = [
        i for i in range(width)
        if header[i] or any(row[i] for row in data)
    ]
    if not keep_idx:
        return None

    raw_names = [
        header[i] if header[i] else f"col_{i + 1}"
        for i in keep_idx
    ]
    names = _unique_names([safe_identifier(n, f"col_{i + 1}", "c_") for i, n in enumerate(raw_names)])

    columns: List[Tuple[str, str]] = []
    column_values: List[List[object]] = []
    for pos, idx in enumerate(keep_idx):
        values = [row[idx] for row in data]
        col_type = _infer_type(values)
        coerced: List[object] = []
        for v in values:
            cv, ok = _coerce(v, col_type)
            coerced.append(cv)
            if not ok:
                col_type = "TEXT"
        if col_type == "TEXT":
            coerced = [None if _cell_text(v) == "" else _cell_text(v) for v in values]
        columns.append((names[pos], col_type))
        column_values.append(coerced)

    if data:
        data_rows = [
            [column_values[col][row_i] for col in range(len(columns))]
            for row_i in range(len(data))
        ]
    else:
        data_rows = []

    return {
        "columns": columns,
        "rows": data_rows,
        "notes": notes,
        "fallback_name": fallback_name,
    }


# ----------------------------------------------------------------------
# 写库
# ----------------------------------------------------------------------
def _connect_write() -> sqlite3.Connection:
    USER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(USER_DB_PATH), timeout=15)
    con.execute(
        "CREATE TABLE IF NOT EXISTS %s ("
        " name TEXT PRIMARY KEY, display_name TEXT, source_file TEXT, sheet TEXT,"
        " row_count INTEGER, columns_json TEXT, imported_at TEXT)" % REGISTRY_TABLE
    )
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND lower(name)=lower(?)",
        (name,),
    ).fetchone()
    return bool(row)


def _quote(name: str) -> str:
    return '"%s"' % name.replace('"', '""')


def import_file(
    path: Path,
    filename: str,
    table_name: Optional[str] = None,
    replace: bool = False,
) -> dict:
    """把 xlsx/csv 导入用户库，返回导入摘要（可展示给前端）。"""
    ext = Path(filename).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        sheets = _read_xlsx_sheets(path)
    elif ext == ".csv":
        sheets = _read_csv_rows(path)
    elif ext in (".xls",):
        raise DataSourceError(
            "旧版 .xls 不支持导入；请另存为 .xlsx 或 .csv 后重试", status_code=400
        )
    else:
        raise DataSourceError(
            f"不支持的数据文件格式 {ext}，请上传 .xlsx/.xlsm/.csv", status_code=400
        )

    stem = safe_identifier(table_name or Path(filename).stem, "uploaded")
    prepared: List[dict] = []
    seen_tables: Dict[str, int] = {}
    for sheet_name, rows in sheets:
        spec = _prepare_sheet(rows, stem)
        if spec is None:
            continue
        if len(sheets) > 1:
            suffix = safe_identifier(sheet_name, "sheet")
            name = safe_identifier(f"{stem}__{suffix}", stem)
        else:
            name = stem
        key = name.lower()
        if key in seen_tables:
            seen_tables[key] += 1
            name = safe_identifier(f"{name}_{seen_tables[key]}", name)
        else:
            seen_tables[key] = 1
        spec["table"] = name
        spec["sheet"] = sheet_name
        if len(spec["rows"]) > MAX_DATASOURCE_ROWS:
            raise DataSourceError(
                f"工作表 {sheet_name} 有 {len(spec['rows'])} 行，超过单表上限 "
                f"{MAX_DATASOURCE_ROWS} 行；请先拆分后再上传",
                status_code=413,
            )
        prepared.append(spec)

    if not prepared:
        raise DataSourceError("未解析到有效数据行（表头之后为空或整表为空）")

    imported = []
    replaced = []
    with _IMPORT_LOCK:
        con = _connect_write()
        try:
            with con:  # 单事务：任一张表失败则整体回滚
                for spec in prepared:
                    name = spec["table"]
                    exists = _table_exists(con, name)
                    if exists and not replace:
                        raise DataSourceError(
                            f"表 {name} 已存在；如需覆盖请带 replace=true 重新上传",
                            status_code=409,
                        )
                    if exists:
                        con.execute("DROP TABLE %s" % _quote(name))
                        replaced.append(name)

                    cols_sql = ", ".join(
                        "%s %s" % (_quote(c), t) for c, t in spec["columns"]
                    )
                    con.execute("CREATE TABLE %s (%s)" % (_quote(name), cols_sql))
                    if spec["rows"]:
                        placeholders = ", ".join("?" for _ in spec["columns"])
                        con.executemany(
                            "INSERT INTO %s VALUES (%s)" % (_quote(name), placeholders),
                            spec["rows"],
                        )
                    con.execute(
                        "DELETE FROM %s WHERE lower(name)=lower(?)" % REGISTRY_TABLE,
                        (name,),
                    )
                    con.execute(
                        "INSERT OR REPLACE INTO %s"
                        " (name, display_name, source_file, sheet, row_count, columns_json, imported_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)" % REGISTRY_TABLE,
                        (
                            name,
                            spec["fallback_name"],
                            filename,
                            spec["sheet"],
                            len(spec["rows"]),
                            json.dumps(
                                [{"name": c, "type": t} for c, t in spec["columns"]],
                                ensure_ascii=False,
                            ),
                            datetime.now().isoformat(timespec="seconds"),
                        ),
                    )
                    imported.append({
                        "table": name,
                        "sheet": spec["sheet"],
                        "rows": len(spec["rows"]),
                        "columns": [{"name": c, "type": t} for c, t in spec["columns"]],
                        "notes": spec["notes"],
                    })
        finally:
            con.close()

    return {
        "source_file": filename,
        "tables": imported,
        "replaced": replaced,
        "db": str(USER_DB_PATH),
    }


# ----------------------------------------------------------------------
# 读登记表 / 给 schema_linker 的动态上下文
# ----------------------------------------------------------------------
def _connect_ro() -> Optional[sqlite3.Connection]:
    if not USER_DB_PATH.exists():
        return None
    uri = "file:%s?mode=ro" % USER_DB_PATH.resolve().as_posix()
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None


def list_sources() -> List[dict]:
    con = _connect_ro()
    if con is None:
        return []
    try:
        try:
            rows = con.execute(
                "SELECT name, display_name, source_file, sheet, row_count,"
                " columns_json, imported_at FROM %s ORDER BY imported_at DESC, name"
                % REGISTRY_TABLE
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        con.close()
    result = []
    for name, display, source, sheet, row_count, cols_json, imported_at in rows:
        try:
            columns = json.loads(cols_json or "[]")
        except json.JSONDecodeError:
            columns = []
        result.append({
            "table": name,
            "qualified": f'userdb."{name}"',
            "display_name": display,
            "source_file": source,
            "sheet": sheet,
            "rows": row_count,
            "columns": columns,
            "imported_at": imported_at,
        })
    return result


def delete_source(name: str) -> dict:
    with _IMPORT_LOCK:
        con = _connect_write()
        try:
            with con:
                row = con.execute(
                    "SELECT name FROM %s WHERE lower(name)=lower(?)" % REGISTRY_TABLE,
                    (name,),
                ).fetchone()
                if not row:
                    raise DataSourceError(f"数据表 {name} 不存在", status_code=404)
                real = row[0]
                con.execute("DROP TABLE %s" % _quote(real))
                con.execute(
                    "DELETE FROM %s WHERE name=?" % REGISTRY_TABLE, (real,)
                )
        finally:
            con.close()
    return {"deleted": name, "db": str(USER_DB_PATH)}


def _sample_values(con: sqlite3.Connection, table: str, columns: List[dict]) -> dict:
    samples = {}
    text_cols = [
        c["name"] for c in columns
        if c.get("type") == "TEXT" and not re.search(r"(^|_)id$", c["name"], re.I)
    ][:2]
    for col in text_cols:
        try:
            vals = [
                r[0] for r in con.execute(
                    'SELECT DISTINCT "%s" FROM "%s" WHERE "%s" IS NOT NULL LIMIT 4'
                    % (col.replace('"', '""'), table.replace('"', '""'), col.replace('"', '""'))
                ).fetchall()
            ]
            if vals:
                samples[col] = vals
        except sqlite3.Error:
            continue
    return samples


def load_prompt_assets() -> Tuple[List[dict], Dict[str, str], str]:
    """返回 (关键词条目, 表分节, 提示词段落)。任何异常都降级为空。"""
    try:
        sources = list_sources()
    except Exception:
        logger.warning("读取用户数据表失败，按无上传表处理", exc_info=True)
        return [], {}, ""
    if not sources:
        return [], {}, ""

    con = _connect_ro()
    entries: List[dict] = []
    sections: Dict[str, str] = {}
    blocks: List[str] = [
        "## 四、用户上传数据表（附加库 userdb，可直接问数）",
        "以下表来自用户上传的 Excel/CSV，位于 SQLite 附加库 `userdb`。",
        '查询时必须写成 `userdb."表名"`（例如 `SELECT * FROM userdb."订单"`），'
        "不要写成主库裸表名；列名用双引号包裹。",
    ]
    try:
        for src in sources:
            table = src["table"]
            qualified = src["qualified"]
            cols = src["columns"]
            col_desc = "、".join(f'`{c["name"]}` {c["type"]}' for c in cols)
            samples = _sample_values(con, table, cols) if con is not None else {}

            block_lines = [
                '### `%s`（用户上传：%s；sheet=%s；%d 行）'
                % (qualified, src["source_file"], src["sheet"], src["rows"] or 0),
                f"- 列：{col_desc}",
            ]
            for col, vals in samples.items():
                block_lines.append(f"- 示例值 {col} = {vals}")
            sections[qualified] = "\n".join(block_lines)
            blocks.extend(block_lines)
            blocks.append("")

            keywords = [table, src["source_file"], Path(src["source_file"]).stem]
            keywords += [c["name"] for c in cols if len(c["name"]) >= 2]
            keywords += [p for p in table.split("_") if len(p) >= 2]
            cleaned = []
            for kw in keywords:
                kw = (kw or "").strip()
                if kw and kw not in cleaned:
                    cleaned.append(kw)
            entries.append({
                "table": qualified,
                "business_name": "%s（用户上传）" % src["source_file"],
                "keywords": cleaned,
                "related": [],
            })
    finally:
        if con is not None:
            con.close()

    return entries, sections, "\n".join(blocks).strip()
