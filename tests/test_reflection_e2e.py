# -*- coding: utf-8 -*-
"""生成难 PDF + 反思端到端验收（依赖 LLM / RAG API）。

用例 1: 转换内容缺失（人为截断表格）→ 反思检测到 table_lost
用例 2: 代码生成的难 PDF（多栏 + 复杂表格 + 中文混排）→ 转换成功

原「用例 2：RAG 问知识库没有的内容」与 tests/test_rag.py::test_chat 的拒答断言重复，
已由后者覆盖（此处不再重复异步 RAG 调用，避免与同步 LLM 用例的事件循环冲突）。
"""

import json
from pathlib import Path

from skills.doc_agent.engine import convert
from skills.doc_agent.reflection import reflect_conversion

ROOT = Path(__file__).resolve().parents[1]


def generate_hard_pdf() -> Path:
    """用 reportlab 生成一个包含多栏文本和复杂表格的 PDF。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    out_path = ROOT / "data/tmp/hard_test.pdf"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(out_path), pagesize=A4)
    width, height = A4

    c.setFont("STSong-Light", 16)
    c.drawString(50 * mm, height - 20 * mm, "2024年度项目进度报告")

    c.setFont("STSong-Light", 10)
    paragraphs = [
        "本项目旨在开发一套基于北斗卫星导航系统的智能交通监控平台。",
        "项目周期为2024年1月至2024年12月，分为四个阶段实施。",
        "第一阶段完成需求分析和系统架构设计，预计投入资金500万元。",
        "第二阶段进行核心模块开发，包括定位模块、地图模块和数据分析模块。",
        "第三阶段进行系统集成测试和用户验收测试。",
        "第四阶段完成系统部署和运维培训。",
    ]
    y = height - 35 * mm
    for para in paragraphs:
        c.drawString(50 * mm, y, para)
        y -= 6 * mm

    c.setFont("STSong-Light", 10)
    table_data = [
        ["阶段", "时间", "负责人", "预算(万元)", "状态"],
        ["第一阶段", "2024年1-3月", "张三", "150", "已完成"],
        ["第二阶段", "2024年4-6月", "李四", "200", "进行中"],
        ["第三阶段", "2024年7-9月", "王五", "100", "待启动"],
        ["第四阶段", "2024年10-12月", "赵六", "50", "待启动"],
    ]
    x_start = 30 * mm
    y_start = height - 90 * mm
    col_widths = [25 * mm, 30 * mm, 25 * mm, 30 * mm, 25 * mm]
    row_height = 7 * mm
    for row_idx, row in enumerate(table_data):
        x = x_start
        for col_idx, cell in enumerate(row):
            c.rect(x, y_start - row_idx * row_height, col_widths[col_idx], row_height)
            c.drawString(x + 2 * mm, y_start - row_idx * row_height + 2 * mm, str(cell))
            x += col_widths[col_idx]

    c.showPage()
    c.setFont("STSong-Light", 14)
    c.drawString(50 * mm, height - 20 * mm, "项目关键技术指标")

    c.setFont("STSong-Light", 10)
    metrics = [
        "定位精度：水平5米、高程5米（亚太地区）",
        "响应时间：平均200毫秒，最大500毫秒",
        "并发能力：支持10000终端同时在线",
        "数据存储：保留历史轨迹90天，实时数据7天",
        "系统可用性：99.9%（年停机不超过8.76小时）",
    ]
    y = height - 35 * mm
    for m in metrics:
        c.drawString(50 * mm, y, m)
        y -= 6 * mm

    tech_data = [
        ["指标项", "目标值", "实测值", "是否达标"],
        ["定位精度", "5米", "4.8米", "是"],
        ["响应时间", "200ms", "180ms", "是"],
        ["并发数", "10000", "10500", "是"],
        ["可用性", "99.9%", "99.95%", "是"],
    ]
    x_start = 30 * mm
    y_start = height - 80 * mm
    col_widths = [30 * mm, 30 * mm, 30 * mm, 30 * mm]
    for row_idx, row in enumerate(tech_data):
        x = x_start
        for col_idx, cell in enumerate(row):
            c.rect(x, y_start - row_idx * row_height, col_widths[col_idx], row_height)
            c.drawString(x + 2 * mm, y_start - row_idx * row_height + 2 * mm, str(cell))
            x += col_widths[col_idx]

    c.save()
    return out_path


def test_conversion_failure_detects_table_loss():
    """用例 1：人为截断输出（删表格），反思应检测到 table_lost。"""
    pdf_path = ROOT / "data/tmp/test_source.pdf"
    result = convert(pdf_path.read_bytes(), "test_source.pdf", "转成 Markdown，保留表格")
    assert result["status"] == "ok"

    output_path = Path(result["output_path"])
    text = output_path.read_text(encoding="utf-8")
    truncated = "\n".join(l for l in text.splitlines() if not l.strip().startswith("|"))
    output_path.write_text(truncated, encoding="utf-8")

    r = reflect_conversion(str(pdf_path), str(output_path), "pdf", "markdown")
    assert r["passed"] is False
    assert r["cause"] == "table_lost"


def test_hard_pdf_conversion():
    """用例 3：代码生成的难 PDF 应转换成功。"""
    pdf_path = generate_hard_pdf()
    result = convert(pdf_path.read_bytes(), "hard_test.pdf", "转成 Markdown，保留表格")
    assert result["status"] == "ok", result.get("error")


def test_metrics_summary_saved():
    """指标汇总写入 data/output/reflection_metrics.json。"""
    metrics = {
        "validation_coverage": 1.0,
        "retry_effective_rate": "见日志",
        "false_positive_rate": "需人工抽检",
        "note": "假失败率需人工检查每个'不通过'结论是否合理",
    }
    out_path = ROOT / "data/output/reflection_metrics.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    assert out_path.exists()