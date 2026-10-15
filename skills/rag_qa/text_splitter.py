"""文本分块：RecursiveCharacterTextSplitter，中文友好。

支持两种模式：
- 单层分块（split_text_with_positions）：CHUNK_SIZE=500，用于 USE_PARENT_CHILD=false 的回退路径。
- Small-to-Big 父子双层分块：先切父块（split_parent_chunks，1000/overlap=0），
  再在父块内切小块（split_child_positions，200/overlap=50），
  小块全局起始位置 = 父块起始位置 + 父块内偏移，页码定位不受影响。
"""

from langchain_text_splitters import RecursiveCharacterTextSplitter

from skills.rag_qa.config import (
    CHILD_CHUNK_OVERLAP,
    CHILD_CHUNK_SIZE,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    PARENT_CHUNK_SIZE,
)

# 中文友好的分隔符：先按段落/换行/句子，最后按字符
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ",", " ", ""]


def get_splitter(
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> RecursiveCharacterTextSplitter:
    """获取文本分块器。"""
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
        length_function=len,
    )


def split_text(text: str) -> list[str]:
    """将文本切分为块列表。"""
    splitter = get_splitter()
    return splitter.split_text(text)


def split_text_with_positions(text: str) -> list[tuple[str, int]]:
    """将文本切分为块，并返回每块在原文中的起始字符位置。

    用 add_start_index=True 的分块器 create_documents 实现，
    起始位置供 PDF 分块定位页码使用（locate_pages）。

    返回:
        [(块文本, 起始字符位置), ...]
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=_SEPARATORS,
        length_function=len,
        add_start_index=True,
    )
    docs = splitter.create_documents([text])
    return [(d.page_content, d.metadata.get("start_index", 0)) for d in docs]


def split_parent_chunks(text: str) -> list[tuple[str, int]]:
    """Small-to-Big 父块切分：PARENT_CHUNK_SIZE=1000，overlap=0。

    overlap=0 是刻意设计：父块间无重叠，避免重叠区域在相邻父块中
    各切一次产生重复小块；上下文连续性由小块自身的 overlap 保证。

    返回:
        [(父块文本, 起始字符位置), ...]
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=PARENT_CHUNK_SIZE,
        chunk_overlap=0,
        separators=_SEPARATORS,
        length_function=len,
        add_start_index=True,
    )
    docs = splitter.create_documents([text])
    return [(d.page_content, d.metadata.get("start_index", 0)) for d in docs]


def split_child_positions(parent_text: str) -> list[tuple[str, int]]:
    """在单个父块内切小块：CHILD_CHUNK_SIZE=200，overlap=50。

    返回的起始位置是父块内偏移，小块全局起始位置 = 父块起始位置 + 此偏移。

    返回:
        [(小块文本, 父块内起始偏移), ...]
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHILD_CHUNK_SIZE,
        chunk_overlap=CHILD_CHUNK_OVERLAP,
        separators=_SEPARATORS,
        length_function=len,
        add_start_index=True,
    )
    docs = splitter.create_documents([parent_text])
    return [(d.page_content, d.metadata.get("start_index", 0)) for d in docs]
