"""RAG 端到端测试：入库 → 问答（迁自 text-converter-assistant/test_rag.py）。

使用独立测试用户 pytest_rag（kb_pytest_rag collection），不污染默认知识库。
运行前请确保 .env 中已填入有效的 LLM_API_KEY 和 EMBED_API_KEY。
"""

import asyncio
import sys

from skills.rag_qa import rag_engine
from skills.rag_qa.document_loader import extract_text
from skills.rag_qa.text_splitter import split_text

TEST_USER = "pytest_rag"
TEST_DOC = "中文测试文档.md"


def test_parse_and_chunk():
    """步骤 1：文档解析与分块（不依赖 API）。"""
    print("=" * 60)
    print("[步骤1] 文档解析与分块")
    with open(TEST_DOC, "rb") as f:
        content = f.read()
    text = extract_text(content, TEST_DOC)
    print(f"  文档总字数: {len(text)}")
    chunks = split_text(text)
    print(f"  分块数: {len(chunks)}")
    for i, c in enumerate(chunks):
        print(f"  chunk {i}: {len(c)} 字, 开头: {c[:40]}...")
    print("  ✅ 解析+分块通过")
    return chunks


def test_ingest():
    """步骤 2：入库（需要 embedding API；父子分块模式）。"""
    print("=" * 60)
    print("[步骤2] 文档入库（Small-to-Big 父子分块）")
    with open(TEST_DOC, "rb") as f:
        content = f.read()
    result = rag_engine.ingest(
        content, filename=TEST_DOC, user=TEST_USER, replace=True,
        doc_date="2026-10-01", doc_version="v2.0",
    )
    print(f"  入库结果: {result}")
    assert result["chunk_mode"] == "parent_child", "应启用父子分块模式"
    assert result["user"] == TEST_USER
    store = rag_engine.get_store(TEST_USER)
    print(f"  向量库当前总块数: {store.count()}")
    docs = store.list_documents()
    assert any(d["source"] == TEST_DOC for d in docs)
    assert any(d["parents"] > 0 for d in docs), "父块应已入库"
    print("  ✅ 入库通过")
    return result


async def test_chat():
    """步骤 3：问答（需要 LLM API；验证引用带仲裁字段）。"""
    print("=" * 60)
    print("[步骤3] 问答测试")

    # 问题 1：文档里明确有的内容
    q1 = "北斗三号系统是什么时候正式开通全球服务的？"
    print(f"\n  问题1: {q1}")
    r1 = await rag_engine.chat(q1, user=TEST_USER)
    print(f"  回答: {r1['answer']}")
    print(f"  引用: {[c['file'] + '#' + str(c['chunk']) for c in r1['citations']]}")
    # 空格不敏感断言（模型可能输出 "2020 年 7 月 31 日"）
    answer_flat = "".join(r1["answer"].split())
    assert "2020年7月31日" in answer_flat or "2020-07-31" in answer_flat
    # 入库时指定了 doc_date/doc_version，引用应携带仲裁字段
    assert any(c.get("doc_date") == "2026-10-01" for c in r1["citations"]), \
        "引用应携带 doc_date 仲裁字段"

    # 问题 2：文档里没有的内容（应回答不知道）
    q2 = "北斗系统的总设计师是谁？"
    print(f"\n  问题2: {q2}")
    r2 = await rag_engine.chat(q2, user=TEST_USER)
    print(f"  回答: {r2['answer']}")
    assert "无法回答" in r2["answer"], "无依据问题应拒答"

    # 问题 3：带历史的多轮对话
    q3 = "它的定位精度在亚太地区是多少？"
    print(f"\n  问题3: {q3}")
    history = [
        {"role": "user", "content": q1},
        {"role": "assistant", "content": r1["answer"]},
    ]
    r3 = await rag_engine.chat(q3, history=history, user=TEST_USER)
    print(f"  回答: {r3['answer']}")
    print(f"  引用: {[c['file'] + '#' + str(c['chunk']) for c in r3['citations']]}")
    assert "5" in r3["answer"], "亚太定位精度应为 5 米"

    print("\n  ✅ 问答测试完成")
    return r1, r2, r3


if __name__ == "__main__":
    print("RAG 端到端测试开始\n")

    # 1. 解析+分块
    test_parse_and_chunk()

    # 2. 入库
    try:
        test_ingest()
    except Exception as e:
        print(f"  ❌ 入库失败: {e}")
        print("  请检查 .env 中的 EMBED_API_KEY 是否正确配置。")
        sys.exit(1)

    # 3. 问答
    try:
        asyncio.run(test_chat())
    except Exception as e:
        print(f"  ❌ 问答失败: {e}")
        print("  请检查 .env 中的 LLM_API_KEY 是否正确配置。")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("🎉 全部测试通过！")
