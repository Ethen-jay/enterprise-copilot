"""转换引擎：通过 LangGraph 状态机编排 Planner → Converter → Reviewer。"""

from pathlib import Path

from skills.doc_agent.graph import AgentState, build_graph

# 全局单例 graph
_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def convert(
    file_bytes: bytes,
    filename: str,
    instruction: str,
) -> dict:
    """执行文档转换（通过 LangGraph 状态机）。

    参数:
        file_bytes: 上传文件的字节内容。
        filename: 原始文件名。
        instruction: 自然语言指令。

    返回:
        {
            "status": "ok" | "error",
            "plan": {...},
            "output_path": str,
            "output_filename": str,
            "validation": {...},
            "error": str (仅失败时),
            "retry_count": int,
        }
    """
    graph = get_graph()

    initial_state: AgentState = {
        "instruction": instruction,
        "filename": filename,
        "file_bytes": file_bytes,
        "src_path": "",
        "plan": None,
        "output_path": None,
        "validation": None,
        "error": None,
        "retry_count": 0,
        "status": "ok",
        "retry_hint": None,
        "used_adjustments": [],
        "reflection_log": [],
        "memory_recall": None,
    }

    result = graph.invoke(initial_state)

    # 清理临时源文件
    try:
        if result.get("src_path"):
            Path(result["src_path"]).unlink(missing_ok=True)
    except Exception:
        pass

    # 组装返回结果
    plan = result.get("plan")
    plan_dict = plan.to_dict() if plan else {}

    output_path = result.get("output_path")
    output_filename = Path(output_path).name if output_path else ""

    return {
        "status": result.get("status", "error"),
        "plan": plan_dict,
        "output_path": output_path or "",
        "output_filename": output_filename,
        "validation": result.get("validation") or {},
        "error": result.get("error") or "",
        "retry_count": result.get("retry_count", 0),
        "reflection_log": result.get("reflection_log") or [],
    }
