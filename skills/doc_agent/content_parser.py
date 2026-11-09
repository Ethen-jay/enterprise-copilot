"""共用解析层：校验器和转换器共用的内容指标提取。

用 jieba 分词提取关键词，用 pdfplumber/python-docx 统一段落数和表格行数。
"""

from collections import Counter
from pathlib import Path
from typing import List

import re
import statistics

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

# 段落边界的文本特征：
# - 段末标点收尾 → 该视觉行是一段的最后一行
# - 悬空标点收尾（顿号/逗号/分号/箭头等）→ 句子没说完，必是段内折行
#   （冒号除外：段末冒号常用来引出下一个段落，不能当作续行证据）
# - 项目符号/编号开头 → 新段落起点，即使行间距比段内折行大不了多少
_SENTENCE_END = ("。", "！", "？", "!", "?")
_CONTINUATION_END = (
    "、", "，", "；", "→", ",", ";", "-", "(", "（",
)
_BLOCK_START = (
    "⚫", "●", "•", "·", "▪", "■", "□", "◆", "◇", "▶", "►",
    "- ", "* ", "+ ",
)

# 图片口径（抽取与校验共用，保证源/输出两端一致）：
# - 短边 < 6pt 或面积 < 200pt² 视为分隔线/极小装饰图，不计为图片
# - 同一页 bbox 重叠超过 60% 视为重复图层（描边/阴影叠画），只保留较大的一个
_MIN_IMAGE_SIDE = 6.0
_MIN_IMAGE_AREA = 200.0
_IMAGE_DUPLICATE_IOU = 0.6


def _bbox_iou(a: dict, b: dict) -> float:
    """两个 bbox（x0/top/x1/bottom）的交并比。"""
    inter_w = max(0.0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]))
    inter_h = max(0.0, min(a["bottom"], b["bottom"]) - max(a["top"], b["top"]))
    inter = inter_w * inter_h
    if inter <= 0:
        return 0.0
    area_a = (a["x1"] - a["x0"]) * (a["bottom"] - a["top"])
    area_b = (b["x1"] - b["x0"]) * (b["bottom"] - b["top"])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _image_area(image: dict) -> float:
    return (image["x1"] - image["x0"]) * (image["bottom"] - image["top"])


def _select_content_images(images: List[dict]) -> List[dict]:
    """过滤装饰图并对重复图层去重，返回按阅读顺序（top, x0）排序的图片。

    抽取与计数都走这里，避免“源端算 7 张、输出端算 6 张”的口径漂移。
    """
    ordered = sorted(images, key=lambda im: (im["top"], im["x0"]))
    kept: List[dict] = []
    for image in ordered:
        width = image["x1"] - image["x0"]
        height = image["bottom"] - image["top"]
        if width < _MIN_IMAGE_SIDE or height < _MIN_IMAGE_SIDE:
            continue
        if width * height < _MIN_IMAGE_AREA:
            continue

        duplicate_index = None
        for idx, exist in enumerate(kept):
            if _bbox_iou(image, exist) > _IMAGE_DUPLICATE_IOU:
                duplicate_index = idx
                break

        if duplicate_index is None:
            kept.append(image)
        elif _image_area(image) > _image_area(kept[duplicate_index]):
            kept[duplicate_index] = image

    return sorted(kept, key=lambda im: (im["top"], im["x0"]))


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
        return {
            "paragraph_count": 0,
            "table_row_count": 0,
            "image_count": 0,
            "keywords": [],
        }

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
    elif fmt == "csv":
        return _parse_csv(path)
    elif fmt in ("txt", "text"):
        return _parse_text(path.read_text(encoding="utf-8"))
    else:
        # 尝试当文本读
        try:
            text = path.read_text(encoding="utf-8")
            return _parse_text(text)
        except Exception:
            return {"paragraph_count": 0, "table_row_count": 0, "keywords": []}


def _in_bbox(word: dict, bbox) -> bool:
    """判断词的几何中心是否落在 bbox(x0, top, x1, bottom) 内。"""
    x0, top, x1, bottom = bbox
    cx = (word["x0"] + word["x1"]) / 2
    cy = (word["top"] + word["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


def _group_visual_lines(words: List[dict]) -> List[dict]:
    """按垂直位置把词聚成视觉行。"""
    lines: List[dict] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        height = max(w["bottom"] - w["top"], 1.0)
        cy = (w["top"] + w["bottom"]) / 2
        for line in lines:
            if abs(cy - line["cy"]) <= max(line["height"], height) * 0.5:
                line["words"].append(w)
                line["top"] = min(line["top"], w["top"])
                line["bottom"] = max(line["bottom"], w["bottom"])
                line["height"] = max(line["height"], height)
                line["count"] += 1
                line["cy"] = sum(
                    (x["top"] + x["bottom"]) / 2 for x in line["words"]
                ) / line["count"]
                break
        else:
            lines.append({
                "words": [w],
                "top": w["top"],
                "bottom": w["bottom"],
                "cy": cy,
                "height": height,
                "count": 1,
            })
    lines.sort(key=lambda l: l["top"])
    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
        line["text"] = "".join(w["text"] for w in line["words"])
    return lines


def _adaptive_gap_threshold(gaps: List[float]) -> float | None:
    """从本页行间距分布推断“段内折行”与“段间空行”的分界。

    PDF 里的长段落会被折成多个视觉行：段内折行间距小于段间间距，间距分布
    呈双峰。在 P20–P75 区间内找最早的一次明显间距跳变（即两簇之间的谷），
    取跳变两侧的中点作为阈值；跳变本身要足够大，才认为确实存在折行。

    间距均匀的 PDF（每行本身就是一个段落）没有这种跳变，返回 None，
    调用方按视觉行计数，避免把真实段落并成一个。
    """
    if len(gaps) < 3:
        return None

    ordered = sorted(gaps)
    n = len(ordered)
    lo = ordered[max(int(n * 0.2) - 1, 0)]
    hi = ordered[min(int(n * 0.75), n - 1)]

    # 取"第一个明显跳变"而不是"最大跳变"：段内折行间距总在最前面成簇，
    # 段间空行、图片/标题造成的更大空隙都在其后。若按最大跳变选，遇到
    # 图片这类超大空隙时会把切分点抬到段间之上，整段文字被误合并。
    base_jump = max(0.8, statistics.median(ordered) * 0.08)
    for prev, cur in zip(ordered, ordered[1:]):
        if prev < lo or cur > hi:
            continue
        if cur - prev >= base_jump:
            return (prev + cur) / 2

    return None


def _line_text(line: dict) -> str:
    """取视觉行文本：优先用现成的 text，其次拼接词文本。"""
    text = line.get("text")
    if text is not None:
        return text
    return "".join(w["text"] for w in line.get("words", []))


def _is_paragraph_break(prev_line: dict, line: dict, threshold: float) -> bool:
    """判断两个相邻视觉行之间是否为段落边界。

    只看间距不可靠：同一份 PDF 不同页/版块的字号行高不同，段内折行的
    间距可能和段间间距重叠。因此先用文本特征定性：
    - 本行以项目符号/编号开头 → 断段
    - 上一行以句末标点收尾 → 断段
    - 上一行以悬空标点（、，；：→ 等）收尾 → 句子没说完，续段
    文本上无法判断时，才用本页自适应间距阈值。
    """
    cur_text = _line_text(line).lstrip()
    if cur_text.startswith(_BLOCK_START):
        return True

    prev_text = _line_text(prev_line).rstrip()
    if prev_text.endswith(_SENTENCE_END):
        return True
    if prev_text.endswith(_CONTINUATION_END):
        return False

    return (line["top"] - prev_line["bottom"]) > threshold


def _group_paragraph_lines(
    lines: List[dict],
    threshold: float | None,
) -> List[List[dict]]:
    """把视觉行按段落合并，返回每段的视觉行列表。

    threshold 为 None 时每行即一段（间距均匀，无折行特征）。
    """
    if not lines:
        return []
    if threshold is None:
        return [[line] for line in lines]

    groups: List[List[dict]] = [[lines[0]]]
    for prev, line in zip(lines, lines[1:]):
        if _is_paragraph_break(prev, line, threshold):
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups


def _count_paragraphs(lines: List[dict], threshold: float | None) -> int:
    """统计视觉行合并后的段落数。"""
    return len(_group_paragraph_lines(lines, threshold))


def _parse_pdf(path: Path) -> dict:
    """PDF: 按版式位置统计段落数 + 表格行数。

    表格 bbox 内的词归入表格行，正文词按视觉行聚合后再按行间距合并成段，
    避免长段落折行被逐行计成多段。
    """
    import pdfplumber
    table_row_count = 0
    fallback_paras = 0
    image_count = 0
    all_text = []
    page_lines: List[List[dict]] = []

    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_text.append(text)
            image_count += len(_select_content_images(page.images or []))

            # pdfplumber 0.11.x 没有 page.words 属性，等价 API 是 extract_words()
            words = page.extract_words() or []
            tables = page.find_tables() or []
            table_bboxes = [tuple(t.bbox) for t in tables]

            for table in tables:
                rows = table.extract() or []
                structured_rows = [
                    row for row in rows
                    if row and any((cell or "").strip() for cell in row)
                ]
                if structured_rows:
                    table_row_count += len(structured_rows)
                else:
                    inner_words = [w for w in words if _in_bbox(w, table.bbox)]
                    table_row_count += len(_group_visual_lines(inner_words))

            body_words = [
                w for w in words
                if not any(_in_bbox(w, bbox) for bbox in table_bboxes)
            ]
            lines = _group_visual_lines(body_words)
            page_lines.append(lines)
            if text.strip() and not words:
                # 无词级信息的扫描/加密页：退回按文本行计数
                fallback_paras += len([l for l in text.splitlines() if l.strip()])

    # 行距按页推断：同一份 PDF 不同页/版块的版式密度可能不同，
    # 用全文统一阈值会把某一页的段内折行误判成段间空行。
    para_count = fallback_paras
    for lines in page_lines:
        page_gaps = [
            lines[idx]["top"] - lines[idx - 1]["bottom"]
            for idx in range(1, len(lines))
        ]
        para_count += _count_paragraphs(
            lines, _adaptive_gap_threshold(page_gaps)
        )

    full_text = "\n".join(all_text)
    return {
        "paragraph_count": para_count,
        "table_row_count": table_row_count,
        "image_count": image_count,
        "keywords": extract_keywords(full_text),
    }


def _parse_docx(path: Path) -> dict:
    """DOCX: python-docx 提取段落数 + 表格行数 + 图片数。"""
    from docx import Document
    doc = Document(str(path))
    para_count = len([p for p in doc.paragraphs if p.text.strip()])
    table_row_count = 0
    # 图片按内联图形计：pandoc 插入的图片段落都是 inline shape，
    # 图片段落本身没有文字，因此不会影响段落口径。
    image_count = len(doc.inline_shapes)
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
        "image_count": image_count,
        "keywords": extract_keywords(" ".join(all_text)),
    }


def _parse_markdown(path: Path) -> dict:
    """Markdown: 正则统计段落 + Markdown 表格行 + 图片引用。"""
    import re
    text = path.read_text(encoding="utf-8")
    image_line = re.compile(r"^\s*!\[[^\]]*\]\(([^)]+)\)\s*$")
    lines = text.splitlines()
    # 图片单独计入 image_count，不能算正文段落，否则段落口径会漂；
    # 只统计能真正解析到图片文件的引用（见 _image_ref_readable）
    image_count = len([
        l for l in lines
        if (m := image_line.match(l)) and _image_ref_readable(m.group(1), path.parent)
    ])
    # 段落：非空行（不含表格行、不含图片行）
    para_lines = [
        l for l in lines
        if l.strip() and not l.strip().startswith("|") and not image_line.match(l)
    ]
    para_count = len(para_lines)
    # 表格行：以 | 开头的行（不含分隔线）
    table_lines = [l for l in lines if l.strip().startswith("|") and "---" not in l]
    table_row_count = len(table_lines)

    return {
        "paragraph_count": para_count,
        "table_row_count": table_row_count,
        "image_count": image_count,
        # 图片引用（含 alt 文本与 assets 路径）不参与关键词统计，
        # 否则"图片"这类 alt 会把正文的高频词挤出 top_n，关键词保留率假性下降
        "keywords": extract_keywords(re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)),
    }


def _parse_html(path: Path) -> dict:
    """HTML: 统计块级文本单元（段落/标题/列表项）和表格行。"""
    text = path.read_text(encoding="utf-8")
    import re

    # 只含图片的 <p> 不是正文段落（与 pdf/docx/markdown 的图片口径一致），
    # 先剔除再统计，否则每张图都会让段落数虚增一段
    without_images = re.sub(
        r"<p\b[^>]*>\s*(?:<img\b[^>]*>\s*)+</p>", "", text, flags=re.IGNORECASE
    )

    # 段落、标题、列表项、定义列表项等块级文本单元
    para_count = len(re.findall(r"<p\b[^>]*>", without_images, re.IGNORECASE))
    para_count += len(re.findall(r"<h[1-6]\b[^>]*>", text, re.IGNORECASE))
    # li 内若已含 p，由上面的 p 计数覆盖，避免重复
    para_count += len(re.findall(r"<li\b[^>]*>(?!\s*<p\b)", text, re.IGNORECASE))
    para_count += len(re.findall(r"<dt\b[^>]*>", text, re.IGNORECASE))
    para_count += len(re.findall(r"<dd\b[^>]*>", text, re.IGNORECASE))
    # 表格行
    rows = re.findall(r"<tr[^>]*>.*?</tr>", text, re.DOTALL | re.IGNORECASE)
    table_row_count = len(rows)
    # 图片：<img> 不是块级文本单元，不影响段落口径；
    # 只统计能解析到真实图片的引用（data URI / 本地存在），与 Markdown 同口径
    image_count = len([
        src for src in re.findall(r'<img\b[^>]*?\bsrc="([^"]+)"', text, re.IGNORECASE)
        if _image_ref_readable(src, path.parent)
    ])

    # 去标签提取纯文本
    clean = re.sub(r"<[^>]+>", " ", text)
    return {
        "paragraph_count": para_count,
        "table_row_count": table_row_count,
        "image_count": image_count,
        "keywords": extract_keywords(clean),
    }


def _image_ref_readable(target: str, base_dir: Path) -> bool:
    """判断 Markdown/HTML 里的图片引用是否指向真实可用的图片。

    文件缺失的相对路径、以及远程 URL，都不计入 image_count：前者说明源文件
    本身没带图（例如只上传了我们导出的 .md，assets/ 不在身边），后者目标格式
    不保证能取到，计进去只会产生"图片丢失"的假警报。
    """
    target = (target or "").strip().strip("\"'")
    if not target:
        return False
    if target.startswith("data:"):
        return True
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target):
        return False
    path = Path(target)
    if not path.is_absolute():
        path = base_dir / path
    return path.is_file()


def _parse_xlsx(path: Path) -> dict:
    """XLSX: 单列行计为正文段落，多列行计为表格行。"""
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True)
    para_count = 0
    table_row_count = 0
    all_text = []

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            values = [
                str(c.value) for c in row
                if c.value is not None and str(c.value).strip()
            ]
            if not values:
                continue
            if len(values) == 1:
                para_count += 1
            else:
                table_row_count += 1
            all_text.extend(values)

    wb.close()
    return {
        "paragraph_count": para_count,
        "table_row_count": table_row_count,
        "image_count": 0,
        "keywords": extract_keywords(" ".join(all_text)),
    }


def _parse_csv(path: Path) -> dict:
    """CSV: 首行为表头，其余为数据行；全部按表格行计（与 Markdown 表格口径一致）。"""
    import csv
    import io

    rows = [
        row for row in csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig")))
        if any(cell.strip() for cell in row)
    ]
    return {
        "paragraph_count": 0,
        "table_row_count": len(rows),
        "image_count": 0,
        "keywords": extract_keywords(" ".join(" ".join(row) for row in rows)),
    }


def _parse_text(text: str) -> dict:
    """纯文本解析。"""
    lines = [l for l in text.splitlines() if l.strip()]
    return {
        "paragraph_count": len(lines),
        "table_row_count": 0,
        "image_count": 0,
        "keywords": extract_keywords(text),
    }
