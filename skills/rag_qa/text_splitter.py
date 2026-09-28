"""文本分块：RecursiveCharacterTextSplitter，中文友好。"""

from langchain_text_splitters import RecursiveCharacterTextSplitter

from skills.rag_qa.config import CHUNK_OVERLAP, CHUNK_SIZE

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
