# -*- coding: utf-8 -*-
"""记忆模块验收测试：三个场景（依赖 LLM API）。

① 显式「记住…」→ 存偏好
② 重试成功 → 存任务历史 + 洞察（用 memory_node 直跑模拟状态）
③ 下次规划召回记忆（preferences + recent_tasks + insights）

三个场景有状态依赖（记忆跨场景累积），故用数字前缀保证执行顺序，
并在模块开始时统一清空一次记忆表。
"""

import logging
from pathlib import Path

import pytest

from skills.doc_agent import engine, memory
from skills.doc_agent.graph import AgentState, memory_node

ROOT = Path(__file__).resolve().parents[1]
TEST_PDF = ROOT / "data/tmp/test_source.pdf"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


@pytest.fixture(scope="module", autouse=True)
def _reset_memory_once():
    """模块级：跑三个场景前清空一次记忆表，保证隔离。"""
    memory.reset_for_test()
    yield


def test_01_explicit_preference():
    """场景①：显式「记住…」→ 写入 user_preferences。"""
    instruction = "记住我最喜欢 Markdown 输出，把这个 PDF 转成 Markdown"
    result = engine.convert(TEST_PDF.read_bytes(), "test_source.pdf", instruction)

    prefs = memory.get_preferences()
    assert result["status"] == "ok"
    assert len(prefs) >= 1
    assert any("Markdown" in p["value"] for p in prefs)


def test_02_retry_success_writes_history():
    """场景②：重试成功 → 写入 task_history + insights（直跑 memory_node 模拟状态）。"""
    from skills.doc_agent.planner_v2 import TaskPlan

    mock_state: AgentState = {
        "instruction": "把这个 PDF 转成 Markdown",
        "filename": "test_source.pdf",
        "file_bytes": b"",
        "src_path": "",
        "plan": None,
        "output_path": str(ROOT / "data/output/output_pdf_to_markdown.md"),
        "validation": {"passed": True, "issues": []},
        "error": None,
        "retry_count": 1,
        "status": "ok",
        "retry_hint": None,
        "used_adjustments": ["use_text_strategy"],
        "reflection_log": [
            {"step": "reflection", "passed": False, "cause": "table_lost",
             "adjustment": "use_text_strategy", "issues": ["表格行数不足"]},
            {"step": "reflection", "passed": True, "cause": None,
             "adjustment": None, "issues": []},
        ],
        "memory_recall": None,
    }
    mock_state["plan"] = TaskPlan(
        task_type="convert",
        source_format="pdf",
        target_format="markdown",
        tools=["pdfplumber", "content_parser", "reflection"],
        steps=[{"action": "convert"}],
        expected_output="markdown_file",
        options={},
        raw_instruction="把这个 PDF 转成 Markdown",
        retry_hints=[],
    )

    memory_node(mock_state)

    tasks = memory.get_recent_tasks(limit=5)
    insights = memory.get_relevant_insights(limit=5)

    assert len(tasks) == 1
    assert tasks[0]["retry_count"] == 1
    assert len(insights) == 1
    assert "use_text_strategy" in insights[0]["content"]


def test_03_recall_on_next_planning():
    """场景③：下次规划召回 preferences + recent_tasks + insights。"""
    result = engine.convert(TEST_PDF.read_bytes(), "test_source.pdf", "把这个 PDF 转成 Markdown")

    prefs = memory.get_preferences()
    tasks = memory.get_recent_tasks(limit=5)
    insights = memory.get_relevant_insights(limit=5)

    assert result["status"] == "ok"
    assert len(prefs) >= 1
    assert len(tasks) >= 1
    assert len(insights) >= 1