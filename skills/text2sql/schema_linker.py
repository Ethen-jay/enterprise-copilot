# -*- coding: utf-8 -*-
"""
schema_linker.py —— Schema Linking（阶段 B）

输入：用户的大白话问题
输出：给 LLM 生成 SQL 用的 schema 上下文字符串，只包含：
  1. 22 表全名单（让模型知道边界）
  2. 关键词打分选出的 Top2~4 候选表（含 related 桥接表）+ tables.md 里的真实列说明
  3. 候选表 2~3 个真实 DISTINCT 示例值（从只读 SQLite 现查）
  4. metrics.md 指标口径全文（硬性约束，每问必带）
  5. few_shot.md 中与候选表最相关的若干范例

设计原则（阶段二刻意简单可解释，不上向量库）：
  - 中文关键词直接做子串匹配打分，规则写在 semantic/table_keywords.json，业务可读可改；
  - 只依赖标准库；SQLite 全程只读 URI。

命令行自测（在项目根目录运行）：
    python -m app.schema_linker "客户销售额排名"
"""

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# 路径统一由 config 收口
from .config import DB_PATH, PROJECT_ROOT, SEMANTIC_DIR
from .anchor import get_anchor_date
from . import datasource, metric_dict, schema_scan, userdb

MAX_TABLES = 4          # 候选表上限（Top2~4）
MAX_FEW_SHOTS = 6       # 拼入的范例条数上限
MAX_EXAMPLE_VALUES = 4  # 每个示例列最多取几个 DISTINCT 值

# 示例值自动选列规则（SQLite 库的列没有类型声明，全部靠列名模式判断）：
# 1) 优先挑“低基数枚举列”（状态/类型/分类…）
ENUM_COL_RE = re.compile(
    r"(status|type|category|stage|region|tier|reason|method|warehouse|finish|scope|department|role|rating)$",
    re.I,
)
# 2) 日期列；同表多个时按业务口径白名单优先（生产口径看出站 out_time，不看进站 in_time）
DATE_COL_RE = re.compile(r"(date|time)$", re.I)
PREFERRED_DATE_COLS = ["out_time", "ship_date", "order_date", "due_date", "test_time",
                       "release_date", "update_date", "complaint_date"]
ID_COL_RE = re.compile(r"_id$", re.I)
# 3) 数值/技术列：无枚举列时退化选文本列，要排除这些
NUMERIC_COL_RE = re.compile(
    r"(qty|amount|price|panels|cost|dppm|duration|cycle|fail|count|seq|layer|width|space|"
    r"num|standard|avg|tested|pass|scrap|rework|input|good|open|short|isolation|is_)",
    re.I,
)
# 退化文本列的优先列名（像“名称/备注/编码/摘要”这类有业务含义的短文本）
TEXT_PREFER_RE = re.compile(r"(name|remark|code|summary|text|desc)", re.I)

# 语义层文档（metrics.md / few_shot.md / tables.md）以该日期为示例快照编写。
# 数据滚动更新后锚点会变，拼 prompt 前把文档里的示例日期统一替换为
# 运行时锚点（anchor.py），避免示例 SQL 的日期与 system prompt 注入的
# ANCHOR 打架；数据未更新时锚点仍是 2026-09-24，替换为 no-op，评测基线不变。
SEMANTIC_EXAMPLE_ANCHOR = "2026-09-24"


def apply_anchor(text: str, anchor_date: str) -> str:
    """把语义层文本中的示例基准日替换为运行时锚点（含 SQL 字面量与正文说明）。"""
    return text.replace(SEMANTIC_EXAMPLE_ANCHOR, anchor_date)


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass
class PickedTable:
    table: str
    business_name: str
    score: float                  # 综合得分
    direct: bool                  # True=问题直接命中关键词；False=related 桥接补入
    matched: List[str] = field(default_factory=list)   # 命中的关键词（桥接表为空）
    via: Optional[str] = None     # 桥接来源表名


@dataclass
class LinkResult:
    question: str
    picked: List[PickedTable]
    examples: Dict[str, Dict[str, object]]
    few_shots: List[str]          # 选中的范例编号，如 ["FS12", "FS13"]
    warnings: List[str]
    context_text: str

    @property
    def table_names(self) -> List[str]:
        return [p.table for p in self.picked]


# ----------------------------------------------------------------------
# 1. 读取语义层文件
# ----------------------------------------------------------------------
def load_keywords() -> List[dict]:
    """读取 table_keywords.json，返回 22 个表的关键词配置。"""
    path = SEMANTIC_DIR / "table_keywords.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["tables"]


def load_table_sections() -> Dict[str, str]:
    """解析 tables.md：返回 {表名: 该表的 markdown 分节（标题+行数+列表格）}。"""
    text = (SEMANTIC_DIR / "tables.md").read_text(encoding="utf-8")
    sections: Dict[str, str] = {}
    # 按三级标题切分：### 1. dim_customer ...
    chunks = re.split(r"(?=^###\s+\d+\.)", text, flags=re.M)
    for chunk in chunks:
        m = re.match(r"###\s+\d+\.\s+(\w+)", chunk.strip())
        if m:
            sections[m.group(1)] = chunk.strip()
    return sections


def load_join_hints() -> str:
    """提取 tables.md 末尾的“表间关联速查”整节（很短，每问全量给）。"""
    text = (SEMANTIC_DIR / "tables.md").read_text(encoding="utf-8")
    m = re.search(r"(##\s*四、表间关联速查.*)\Z", text, re.S)
    return m.group(1).strip() if m else ""


def load_metrics() -> str:
    """指标口径全文（四条强制规则每问必带）。"""
    return (SEMANTIC_DIR / "metrics.md").read_text(encoding="utf-8").strip()


def load_few_shots() -> List[dict]:
    """解析 few_shot.md：返回 [{id,title,tables,keywords,text}]。"""
    text = (SEMANTIC_DIR / "few_shot.md").read_text(encoding="utf-8")
    shots = []
    for m in re.finditer(r"(###\s+(FS\d+)\..*?)(?=\n###\s+FS|\Z)", text, re.S):
        block, fs_id = m.group(1), m.group(2)
        tables, keywords = [], []
        tm = re.search(r"-\s*涉及表:\s*(.+)", block)
        if tm:
            tables = [t.strip() for t in tm.group(1).split(",") if t.strip()]
        km = re.search(r"-\s*适用问法:\s*(.+)", block)
        if km:
            keywords = [k.strip() for k in km.group(1).split() if k.strip()]
        shots.append({
            "id": fs_id,
            "title": block.splitlines()[0].strip("# ").strip(),
            "tables": tables,
            "keywords": keywords,
            "text": block.strip(),
        })
    return shots


# ----------------------------------------------------------------------
# 2. 关键词打分选表
# ----------------------------------------------------------------------
def score_and_pick(question: str, kw_entries: List[dict]) -> List[PickedTable]:
    """
    打分规则（刻意简单、可解释）：
      - 问题文本中每命中一个关键词记 1 分，关键词越长略微加权（1 + 0.1×(字数-2)）；
      - 直接命中表按总分降序；
      - 再按其 related 列表补 JOIN 桥表（桥表记 0.5 的引导分，标记 direct=False）；
      - 总数控制在 MAX_TABLES。
    """
    q = question.strip()
    direct: Dict[str, PickedTable] = {}

    for ent in kw_entries:
        matched = [kw for kw in ent["keywords"] if kw in q]
        if not matched:
            continue
        score = sum(1 + 0.1 * max(len(kw) - 2, 0) for kw in matched)
        direct[ent["table"]] = PickedTable(
            table=ent["table"],
            business_name=ent.get("business_name", ""),
            score=round(score, 2),
            direct=True,
            matched=matched,
        )

    # 直接命中表按分降序（同分时按 json 原顺序，sorted 是稳定排序）
    order = {ent["table"]: i for i, ent in enumerate(kw_entries)}
    picked = sorted(direct.values(), key=lambda p: (-p.score, order[p.table]))

    # related 桥接补表：按直接命中表的得分顺序依次补，直到 MAX_TABLES
    picked_names = {p.table for p in picked}
    for src in list(picked):
        if len(picked) >= MAX_TABLES:
            break
        ent = next(e for e in kw_entries if e["table"] == src.table)
        for rel in ent.get("related", []):
            if len(picked) >= MAX_TABLES:
                break
            if rel in picked_names:
                continue
            rel_ent = next(e for e in kw_entries if e["table"] == rel)
            picked.append(PickedTable(
                table=rel,
                business_name=rel_ent.get("business_name", ""),
                score=0.5,
                direct=False,
                via=src.table,
            ))
            picked_names.add(rel)

    return picked


def pick_few_shots(question: str, picked: List[PickedTable],
                   shots: List[dict]) -> List[str]:
    """范例排序：与候选表的交集数 + 适用问法命中数，取前 MAX_FEW_SHOTS 条。"""
    picked_set = {p.table for p in picked}
    scored = []
    for s in shots:
        table_hits = len(picked_set & set(s["tables"]))
        kw_hits = sum(1 for kw in s["keywords"] if kw in question)
        rank = table_hits * 2 + kw_hits
        if rank > 0:
            scored.append((rank, s["id"]))
    scored.sort(key=lambda x: -x[0])
    return [sid for _, sid in scored[:MAX_FEW_SHOTS]]


# ----------------------------------------------------------------------
# 3. 从只读 SQLite 取示例值
# ----------------------------------------------------------------------
def _open_ro(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else (userdb.current_path() or DB_PATH)
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    return sqlite3.connect(uri, uri=True)


def fetch_examples(con: sqlite3.Connection, table_names: List[str]) -> Dict[str, Dict[str, object]]:
    """
    为每张候选表挑 2~3 个有业务含义的示例：
      - 优先低基数枚举列（status/type/category...）取 DISTINCT 前 4 个值；
      - lot_id 特殊：WO/26 两种格式各取一个样例（埋点提醒）；
      - 日期列给 MIN~MAX 范围（帮助模型理解时间窗）。
    任何查询失败都跳过（示例值是辅助信息，不能阻断主流程）。
    """
    result: Dict[str, Dict[str, object]] = {}
    for table in table_names:
        info: Dict[str, object] = {}
        try:
            cols = con.execute('PRAGMA table_info("%s")' % table).fetchall()
            col_names = [c[1] for c in cols]

            # 3.1 枚举列（最多 2 个）
            enum_cols = [n for n in col_names if ENUM_COL_RE.search(n) and not ID_COL_RE.search(n)][:2]
            # 没有枚举列时，退化选一个有业务含义的文本列：
            # 排除 ID/日期/数值技术列，优先名称/备注/编码类列名（宁缺毋滥）
            if not enum_cols:
                candidates = [n for n in col_names
                              if not ID_COL_RE.search(n)
                              and not DATE_COL_RE.search(n)
                              and not NUMERIC_COL_RE.search(n)]
                candidates.sort(key=lambda n: (0 if TEXT_PREFER_RE.search(n) else 1))
                enum_cols = candidates[:1]
            for col in enum_cols:
                vals = [r[0] for r in con.execute(
                    'SELECT DISTINCT "%s" FROM "%s" WHERE "%s" IS NOT NULL LIMIT %d'
                    % (col, table, col, MAX_EXAMPLE_VALUES)).fetchall()]
                if vals:
                    info[col] = vals

            # 3.2 lot_id 双格式样例
            if "lot_id" in col_names:
                samples = []
                for pat in ("WO%", "26%"):
                    row = con.execute(
                        'SELECT lot_id FROM "%s" WHERE lot_id LIKE ? LIMIT 1' % table, (pat,)
                    ).fetchone()
                    if row:
                        samples.append(row[0])
                if samples:
                    info["lot_id 格式样例"] = samples

            # 3.3 日期列范围：多个日期列时按业务口径白名单优先
            date_cols = [n for n in col_names if DATE_COL_RE.search(n) and not ID_COL_RE.search(n)]
            if date_cols:
                preferred = next((c for c in PREFERRED_DATE_COLS if c in date_cols), None)
                col = preferred or date_cols[0]
                mn, mx = con.execute(
                    'SELECT MIN("%s"), MAX("%s") FROM "%s"' % (col, col, table)
                ).fetchone()
                if mn:
                    info["%s 范围" % col] = "%s ~ %s" % (mn, mx)
        except sqlite3.Error:
            continue
        if info:
            result[table] = info
    return result


# ----------------------------------------------------------------------
# 4. 拼接 schema 上下文
# ----------------------------------------------------------------------
def _all_tables_overview(kw_entries: List[dict], has_user_tables: bool = False) -> str:
    if has_user_tables:
        lines = ["**业务库表（只能使用这些表；用户上传表见第四节）：**"]
    else:
        lines = ["**全库 22 张表（只能使用这些表）：**"]
    for ent in kw_entries:
        lines.append("- `%s`：%s" % (ent["table"], ent.get("business_name", "")))
    return "\n".join(lines)


def _examples_block(examples: Dict[str, Dict[str, object]]) -> str:
    if not examples:
        return ""
    lines = ["### 候选表真实示例值（只读 DISTINCT 取样，仅帮助理解取值）"]
    for table, info in examples.items():
        lines.append("- `%s`：" % table)
        for col, val in info.items():
            lines.append("  - %s = %s" % (col, val))
    return "\n".join(lines)


def _build_external_context(question: str, path: Path,
                            anchor_date: Optional[str] = None) -> LinkResult:
    """用户接入库的 schema 上下文：完全由实际扫描结果生成，不读内置语义层。

    内置语义层（tables.md / metrics.md / few_shot.md）描述的是示例库的 22 张表，
    套到用户库会产生幻觉表名，因此这里只给真实表/列、行数与真实示例值。
    """
    scan = schema_scan.scan(path)
    entries: List[dict] = []
    sections: Dict[str, str] = {}
    for table in scan["tables"]:
        name = table["name"]
        cols = table["columns"]
        col_desc = "、".join('`%s` %s' % (c["name"], c.get("type") or "TEXT") for c in cols)
        rows = table.get("rows") or 0
        sections[name] = (
            '### `%s`（%d 行%s）\n- 列：%s'
            % (name, rows, "以上" if table.get("rows_capped") else "", col_desc)
        )
        keywords = [name, Path(name).stem]
        keywords += [p for p in re.split(r"[_\W]+", name) if len(p) >= 2]
        keywords += [c["name"] for c in cols if len(c["name"] or "") >= 2]
        cleaned: List[str] = []
        for kw in keywords:
            kw = (kw or "").strip()
            if kw and kw not in cleaned:
                cleaned.append(kw)
        entries.append({
            "table": name,
            "business_name": "接入库数据表",
            "keywords": cleaned,
            "related": [],
        })

    picked = score_and_pick(question, entries)
    warnings: List[str] = []
    if not picked:
        # 小库直接全量给出，避免“没命中关键词就答不了”；大库保持原告警
        if len(entries) <= 12:
            picked = [
                PickedTable(table=e["table"], business_name=e["business_name"],
                            score=0.1, direct=False)
                for e in entries
            ]
        else:
            warnings.append("未命中任何表/列关键词，候选表为空；请补充更具体的表名或字段名")

    con = _open_ro(path)
    try:
        examples = fetch_examples(con, [p.table for p in picked])
    except sqlite3.Error:
        examples = {}
    finally:
        con.close()

    anchor = anchor_date or get_anchor_date(db_path=path).date
    parts: List[str] = [
        "# Schema 上下文（当前接入库，业务基准日 ANCHOR = '%s'）\n" % anchor,
        "**库内可用表（共 %d 张，只能使用这些表）：**" % scan["included_count"],
    ]
    for table in scan["tables"]:
        parts.append("- `%s`：%d 行 %d 列" % (
            table["name"], table.get("rows") or 0, len(table["columns"])))
    if scan["excluded"]:
        parts.append("\n## 已排除的表（不可查询）")
        for item in scan["excluded"]:
            parts.append("- `%s`：%s" % (item["name"], item["reason"]))
    parts.append("\n## 一、本次问题选中的表（%d 张）\n" % len(picked))
    for p in picked:
        tag = "直接命中" if p.direct else "补充给出"
        hit = "命中词: %s" % "、".join(p.matched) if p.matched else ""
        parts.append("### `%s`（%s）%s" % (p.table, tag, hit))
        parts.append(sections.get(p.table, ""))
        parts.append("")
    ex = _examples_block(examples)
    if ex:
        parts.append(ex)
        parts.append("")
    parts.append(
        "\n## 二、强制规则\n"
        "- 只生成单条只读 SELECT，禁止任何写操作；\n"
        "- 表名、列名必须与上面完全一致（中文/特殊字符用双引号包裹）；\n"
        "- 日期过滤以 ANCHOR='%s' 为数据最新日；\n"
        "- 不确定字段含义时根据列名与示例值判断，不要臆造不存在的列。\n" % anchor
    )
    return LinkResult(
        question=question,
        picked=picked,
        examples=examples,
        few_shots=[],
        warnings=warnings,
        context_text="\n".join(parts),
    )


def build_context(question: str,
                  kw_entries: Optional[List[dict]] = None,
                  table_sections: Optional[Dict[str, str]] = None,
                  shots: Optional[List[dict]] = None,
                  con: Optional[sqlite3.Connection] = None,
                  anchor_date: Optional[str] = None,
                  use_metric_dict: bool = True,
                  db_path: Optional[Path] = None) -> LinkResult:
    """
    主入口：问题 -> LinkResult（含 context_text）。

    anchor_date：业务基准日；None = 运行时查库动态探测（anchor.py）。
    拼入 prompt 的语义层文本（tables/metrics/few_shot）中的示例日期
    会被统一替换为该锚点。
    use_metric_dict：是否注入人工确认的指标口径字典；默认 True，
    仅评测消融时置 False，不影响线上默认行为。
    db_path：当前用户接入的库；省略时取请求绑定的数据源。内置示例库走
    语义层（tables.md / metrics.md / few_shot.md），用户接入库完全按实际
    schema 扫描生成上下文（见 _build_external_context）。
    """
    target = Path(db_path) if db_path else (userdb.current_path() or DB_PATH)
    try:
        is_builtin = target.resolve() == DB_PATH.resolve()
    except OSError:
        is_builtin = False
    if not is_builtin:
        return _build_external_context(question, target, anchor_date=anchor_date)

    kw_entries = kw_entries if kw_entries is not None else load_keywords()
    table_sections = table_sections if table_sections is not None else load_table_sections()
    shots = shots if shots is not None else load_few_shots()
    anchor = anchor_date or get_anchor_date(db_path=target).date

    # 用户上传的 xlsx/csv 表：动态加入选表关键词与分节；
    # 读取失败时降级为只有业务库（不影响原有问数链路）。
    try:
        user_entries, user_sections, user_block = datasource.load_prompt_assets()
    except Exception:
        user_entries, user_sections, user_block = [], {}, ""
    has_user_tables = bool(user_entries)
    if user_entries:
        kw_entries = list(kw_entries) + user_entries
        table_sections = dict(table_sections)
        table_sections.update(user_sections)

    warnings: List[str] = []
    picked = score_and_pick(question, kw_entries)
    if not picked:
        warnings.append("未命中任何表关键词，候选表为空（需补充 table_keywords.json 或人工指定表）")

    own_con = con if con is not None else _open_ro(target)
    try:
        # 用户上传表在独立附加库，示例值已随分节给出，这里只取业务库表
        business_tables = [p.table for p in picked if not p.table.startswith("userdb.")]
        examples = fetch_examples(own_con, business_tables)
    finally:
        if con is None:
            own_con.close()

    picked_shots = pick_few_shots(question, picked, shots)
    shot_map = {s["id"]: s for s in shots}

    # ---- 拼上下文文本（语义层示例日期 -> 运行时锚点）----
    parts: List[str] = []
    parts.append("# Schema 上下文（业务基准日 ANCHOR = '%s'，即数据最新日）\n" % anchor)
    parts.append(_all_tables_overview(
        [e for e in kw_entries if not e["table"].startswith("userdb.")],
        has_user_tables=has_user_tables,
    ))
    parts.append("\n## 一、本次问题选中的表（%d 张）\n" % len(picked))
    for p in picked:
        tag = "直接命中" if p.direct else "JOIN桥接(由 %s 引入)" % p.via
        hit = "命中词: %s" % "、".join(p.matched) if p.matched else ""
        parts.append("### `%s`（%s，得分 %.1f）%s" % (p.table, tag, p.score, hit))
        sec = table_sections.get(p.table)
        parts.append(apply_anchor(sec, anchor) if sec else "（tables.md 中未找到该表分节）")
        parts.append("")

    ex = _examples_block(examples)
    if ex:
        parts.append(ex)
        parts.append("")

    parts.append(apply_anchor(load_join_hints(), anchor))
    parts.append("\n## 二、指标口径与强制规则（必须严格遵守）\n")
    parts.append(apply_anchor(load_metrics(), anchor))

    if picked_shots:
        parts.append("\n## 三、相关 SQL 范例（模仿其口径与写法，不要照抄具体过滤值）\n")
        for sid in picked_shots:
            parts.append(apply_anchor(shot_map[sid]["text"], anchor))
            parts.append("\n---\n")

    # 用户上传表（附加库 userdb）与人工确认的指标口径字典：
    # 均按“有则追加、无则完全不出现”处理，保证没有上传/没有字典时
    # prompt 与原有基线逐字一致。
    if user_block:
        parts.append("\n" + user_block)
    metric_section = metric_dict.load_metric_section() if use_metric_dict else ""
    if metric_section:
        parts.append("\n" + metric_section)

    return LinkResult(
        question=question,
        picked=picked,
        examples=examples,
        few_shots=picked_shots,
        warnings=warnings,
        context_text="\n".join(parts),
    )


# ----------------------------------------------------------------------
# 5. 选表日志（阶段 D 接入 FastAPI 后在控制台可见，方便 Review 选错表没）
# ----------------------------------------------------------------------
def log_link(result: LinkResult) -> None:
    print("=" * 72)
    print("【Schema Linking】问题：%s" % result.question)
    if not result.picked:
        print("  ⚠ 未选出任何表！%s" % ("；".join(result.warnings)))
    for i, p in enumerate(result.picked, 1):
        if p.direct:
            print("  %d. %-26s 直接命中 得分=%-5s 命中词=%s"
                  % (i, p.table, p.score, "、".join(p.matched)))
        else:
            print("  %d. %-26s 桥接补入 由 %s 引入" % (i, p.table, p.via))
    if result.examples:
        print("  示例值：")
        for t, info in result.examples.items():
            print("    - %s: %s" % (t, "；".join("%s=%s" % (k, v) for k, v in info.items())))
    if result.few_shots:
        print("  选用范例：%s" % "、".join(result.few_shots))
    for w in result.warnings:
        print("  ⚠ " + w)
    print("  context_text 长度：%d 字符" % len(result.context_text))
    print("=" * 72)


# ----------------------------------------------------------------------
# 6. 命令行自测
# ----------------------------------------------------------------------
def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print('用法：python app/schema_linker.py "你的问题"')
        print('示例：python app/schema_linker.py "本月准时交付率"')
        return 2
    result = build_context(argv[1])
    log_link(result)
    return 0 if result.picked else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
