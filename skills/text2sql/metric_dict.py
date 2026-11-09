# -*- coding: utf-8 -*-
"""
metric_dict.py —— 指标口径字典（人工确认维护）

用途：把业务口径从「散落在文档里」变成「生成 SQL 时可注入的结构化规则」。
  - 默认文件：skills/text2sql/semantic/metric_dictionary.yaml（可用 METRIC_DICT_PATH 覆盖）；
  - 生成 SQL 前由 schema_linker 追加到 prompt；
  - 文件缺失 / YAML 解析失败 / 条目字段不合法时一律降级为空串，不阻断主链路。

维护路径：
  1. 直接改 YAML（推荐，可写注释、可批量评审）；
  2. 从已入库文档人工确认后，调用 POST /metric-dictionary/from-doc，
     传入约定格式的 Markdown 片段（见 parse_markdown_snippet），
     由服务校验后追加到 YAML，并记录 source / confirmed_by / confirmed_at。
"""

import logging
import re
from datetime import datetime
from typing import List

import yaml

from .config import METRIC_DICT_PATH

logger = logging.getLogger("text2sql.metric_dict")

MAX_METRICS_IN_PROMPT = 12
_REQUIRED_FIELDS = ("name", "definition")


class MetricDictError(Exception):
    """字典写入失败（格式不合法/文件不可写），message 可直接展示。"""


def _load_raw() -> dict:
    if not METRIC_DICT_PATH.exists():
        return {}
    return yaml.safe_load(METRIC_DICT_PATH.read_text(encoding="utf-8")) or {}


def load_metrics() -> List[dict]:
    """读取并校验条目；任何异常返回空列表（降级）。"""
    try:
        data = _load_raw()
        metrics = data.get("metrics") if isinstance(data, dict) else None
        if not isinstance(metrics, list):
            return []
        valid: List[dict] = []
        for item in metrics:
            if not isinstance(item, dict):
                continue
            if not all(item.get(f) for f in _REQUIRED_FIELDS):
                continue
            valid.append(item)
        return valid
    except Exception:
        logger.warning("指标口径字典读取失败，降级为不注入（%s）", METRIC_DICT_PATH, exc_info=True)
        return []


def load_metric_section() -> str:
    """返回可直接拼进 prompt 的口径段落；无有效条目时返回空串。"""
    metrics = load_metrics()
    if not metrics:
        return ""
    lines = [
        "## 指标口径字典（人工确认，共 %d 条）" % len(metrics),
        "> 本节是人工确认后的指标口径；与上文重复的保持一致，若有冲突以本节为准。",
        "> 本节未覆盖的指标仍按上文 metrics.md 的规则执行。",
        "",
    ]
    for m in metrics[:MAX_METRICS_IN_PROMPT]:
        name = str(m.get("name", "")).strip()
        aliases = m.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in re.split(r"[,，、/]", aliases) if a.strip()]
        alias_text = "、".join(str(a) for a in aliases if a)
        lines.append("### %s%s" % (name, "（别名：%s）" % alias_text if alias_text else ""))
        lines.append("- 定义：%s" % str(m.get("definition", "")).strip())
        if m.get("formula"):
            lines.append("- 公式：%s" % str(m["formula"]).strip())
        if m.get("includes_rework") is not None:
            lines.append(
                "- 返修口径：%s"
                % ("计入返修" if m.get("includes_rework") else "不计入返修重过站")
            )
        if m.get("scope"):
            lines.append("- 适用范围：%s" % str(m["scope"]).strip())
        if m.get("source"):
            lines.append("- 口径来源：%s" % str(m["source"]).strip())
        lines.append("")
    return "\n".join(lines).strip()


# ----------------------------------------------------------------------
# 写入：人工确认的 Markdown 片段 → 结构化条目 → 追加 YAML
# ----------------------------------------------------------------------
_FIELD_MAP = {
    "别名": "aliases",
    "aliases": "aliases",
    "定义": "definition",
    "definition": "definition",
    "公式": "formula",
    "formula": "formula",
    "含返修": "includes_rework",
    "返修口径": "includes_rework",
    "适用范围": "scope",
    "口径说明": "scope",
    "scope": "scope",
    "来源": "source",
    "source": "source",
}


def _parse_bool(text: str) -> bool:
    return text.strip().lower() in ("是", "true", "yes", "1", "含", "计入")


def parse_markdown_snippet(snippet: str, source: str = "", confirmed_by: str = "") -> dict:
    """解析约定格式的 Markdown 片段：

    ## 指标：良率
    - 别名：良率、成品率、yield
    - 定义：一次产出合格数 / 投入数
    - 公式：SUM(good_panels) * 1.0 / SUM(input_panels)
    - 含返修：否
    - 适用范围：过站主记录（is_rework_pass = 0）
    - 来源：IC载板制造全流程.docx
    """
    if not snippet or not snippet.strip():
        raise MetricDictError("片段为空")

    name = ""
    for line in snippet.splitlines():
        m = re.match(r"^#{1,4}\s*(?:指标[:：]\s*)?(.+?)\s*$", line.strip())
        if m:
            name = m.group(1).strip()
            break
    fields: dict = {}
    for line in snippet.splitlines():
        m = re.match(r"^\s*[-*]\s*([^:：]+)[:：]\s*(.*)$", line)
        if not m:
            continue
        key_raw, value = m.group(1).strip(), m.group(2).strip()
        key = _FIELD_MAP.get(key_raw) or _FIELD_MAP.get(key_raw.lower())
        if not key:
            continue
        if key == "aliases":
            fields[key] = [a.strip() for a in re.split(r"[,，、/]", value) if a.strip()]
        elif key == "includes_rework":
            fields[key] = _parse_bool(value)
        else:
            fields[key] = value

    if not name:
        name = fields.get("name", "")
    if not name:
        raise MetricDictError("缺少指标名称（示例标题：## 指标：良率）")
    if not fields.get("definition"):
        raise MetricDictError("缺少「定义」字段")
    if not fields.get("source") and source:
        fields["source"] = source
    fields["name"] = name
    fields["confirmed_by"] = confirmed_by or "manual"
    fields["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
    return fields


def append_metric(metric: dict) -> dict:
    """把已校验的条目追加进 YAML；保留原文件其它条目。"""
    if not metric.get("name") or not metric.get("definition"):
        raise MetricDictError("条目必须包含 name 与 definition")
    try:
        data = _load_raw()
        if not isinstance(data, dict):
            data = {}
        metrics = data.get("metrics")
        if not isinstance(metrics, list):
            metrics = []
        # 同名条目视为“更新口径”，避免同一指标重复注入互相矛盾
        metrics = [m for m in metrics if not (isinstance(m, dict) and m.get("name") == metric["name"])]
        metrics.append(metric)
        data["metrics"] = metrics
        data.setdefault("version", 1)
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        METRIC_DICT_PATH.parent.mkdir(parents=True, exist_ok=True)
        METRIC_DICT_PATH.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except MetricDictError:
        raise
    except Exception as e:
        raise MetricDictError(f"写入指标字典失败：{e}") from e
    return metric
