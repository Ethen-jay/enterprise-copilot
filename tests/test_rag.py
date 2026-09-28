# -*- coding: utf-8 -*-
"""RAG 端到端测试：解析分块 → 入库 → 问答。

运行需要 .env 中已配置 EMBED_API_KEY（入库）与 LLM_API_KEY（问答）。
"""

from pathlib import Path

from skills.rag_qa import rag_engine
from skills.rag_qa.document_loader import extract_text
from skills.rag_qa.text_splitter import split_text

ROOT = Path(__file__).resolve().parents[1]


def test_parse_and_chunk():
    """解析与分块：不依赖任何 API。"""
    content = (ROOT / "中文测试文档.md").read_bytes()
    text = extract_text(content, "中文测试文档.md")
    assert len(text) > 0
    chunks = split_text(text)
    assert len(chunks) > 0
    assert all(len(c) > 0 for c in chunks)


def test_ingest():
    """入库：依赖 embedding API。"""
    content = (ROOT / "中文测试文档.md").read_bytes()
    result = rag_engine.ingest(content, filename="中文测试文档.md")
    assert result["chunks"] >= 1
    assert rag_engine.get_store().count() > 0


async def test_chat():
    """问答：依赖 LLM API。验证命中、拒答（不编造）、多轮。"""
    q1 = "北斗三号系统是什么时候正式开通全球服务的？"
    r1 = await rag_engine.chat(q1)

    q2 = "北斗系统的总设计师是谁？"  # 知识库里没有，应拒答
    r2 = await rag_engine.chat(q2)

    q3 = "它的定位精度在亚太地区是多少？"
    history = [
        {"role": "user", "content": q1},
        {"role": "assistant", "content": r1["answer"]},
    ]
    r3 = await rag_engine.chat(q3, history=history)

    assert r1["answer"].strip()
    assert r1["citations"], "问题1 应命中引用来源"
    # 问题2 应明确拒答，不得编造
    assert ("无法回答" in r2["answer"]) or ("根据现有文档" in r2["answer"])
    assert r3["answer"].strip()