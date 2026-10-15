"""RAG 系统配置：从 .env 读取，集中管理所有参数。"""

import os
from pathlib import Path

from dotenv import load_dotenv

# F1 迁移后：本文件位于 skills/rag_qa/config.py，
# parents[2] 为 monorepo 根；共享数据目录统一在根 data/ 下
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 加载 .env
load_dotenv(PROJECT_ROOT / ".env")


def _get(key: str, default: str = "") -> str:
    val = os.getenv(key, default)
    return val.strip() if isinstance(val, str) else val


# ===== Embedding 模型 =====
EMBED_BASE_URL = _get("EMBED_BASE_URL", "https://api.siliconflow.cn/v1")
EMBED_API_KEY = _get("EMBED_API_KEY", "")
EMBED_MODEL = _get("EMBED_MODEL", "BAAI/bge-large-zh-v1.5")

# ===== Reranker 重排模型 =====
RERANK_BASE_URL = _get("RERANK_BASE_URL", "https://api.siliconflow.cn/v1")
RERANK_API_KEY = _get("RERANK_API_KEY", EMBED_API_KEY)
RERANK_MODEL = _get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
USE_RERANK = _get("USE_RERANK", "true").lower() in ("true", "1", "yes")

# ===== 分块与检索参数 =====
CHUNK_SIZE = int(_get("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(_get("CHUNK_OVERLAP", "50"))
TOP_K = int(_get("TOP_K", "3"))
RETRIEVE_CANDIDATES = int(_get("RETRIEVE_CANDIDATES", "10"))  # 重排前的候选数
# 重排相关性阈值：top_k 全部低于该值时判定知识库无依据，直接拒答（防幻觉第一道闸）
RELEVANCE_THRESHOLD = float(_get("RELEVANCE_THRESHOLD", "0.3"))
HISTORY_TURNS = int(_get("HISTORY_TURNS", "3"))
HISTORY_MAX_MSGS = int(_get("HISTORY_MAX_MSGS", "10"))  # 超过此数量触发压缩

# ===== Small-to-Big 父子分块 =====
# 开启后入库切两层：小块（CHILD_CHUNK_SIZE=200，向量检索命中精准）
# + 父块（PARENT_CHUNK_SIZE=1000，命中后回溯，喂给 LLM 提供完整上下文）。
# 小块 metadata 存 parent_id 指向父块；父块 overlap=0 避免重叠区切出重复小块。
# 关闭时回退单层分块（CHUNK_SIZE=500）。
USE_PARENT_CHILD = _get("USE_PARENT_CHILD", "true").lower() in ("true", "1", "yes")
PARENT_CHUNK_SIZE = int(_get("PARENT_CHUNK_SIZE", "1000"))
CHILD_CHUNK_SIZE = int(_get("CHILD_CHUNK_SIZE", "200"))
CHILD_CHUNK_OVERLAP = int(_get("CHILD_CHUNK_OVERLAP", "50"))

# ===== 扫描版 PDF OCR 兜底 =====
# 页文本字符数低于 OCR_MIN_CHARS 视为无文本层（扫描页），触发 RapidOCR 兜底；
# 混合 PDF 按页判断：有文本层的页直接用文本层（快），仅扫描页走 OCR。
ENABLE_OCR = _get("ENABLE_OCR", "true").lower() in ("true", "1", "yes")
OCR_MIN_CHARS = int(_get("OCR_MIN_CHARS", "10"))
OCR_DPI = int(_get("OCR_DPI", "200"))  # 扫描页渲染分辨率，越高越准但越慢

# ===== 外部 API 指数退避重试 =====
# 429 限流 / 5xx 服务端临时错误 / 网络异常（超时、连接失败）自动重试，
# 指数退避 + 全抖动：delay = random(0, min(MAX_DELAY, BASE_DELAY * 2^attempt))。
# 4xx（除 429）属于参数/鉴权错误，重试无意义，直接抛出。
API_MAX_RETRIES = int(_get("API_MAX_RETRIES", "3"))
API_RETRY_BASE_DELAY = float(_get("API_RETRY_BASE_DELAY", "1.0"))
API_RETRY_MAX_DELAY = float(_get("API_RETRY_MAX_DELAY", "30.0"))

# ===== 向量空间与归一化 =====
# bge 官方推荐 cosine + 向量归一化：embedding 出口统一 L2 归一化（幂等），
# 归一化后 L2 与 cosine 排序等价；建 collection 时指定 hnsw:space。
# 注意：Chroma 的 space 建 collection 时固定，已存在的旧 collection 不受影响。
VECTOR_SPACE = _get("VECTOR_SPACE", "cosine")  # cosine / l2 / ip

# ===== 路径 =====
DATA_DIR = PROJECT_ROOT / _get("DATA_DIR", "data")
CHROMA_DIR = PROJECT_ROOT / _get("CHROMA_DIR", "data/chroma")
UPLOADS_DIR = PROJECT_ROOT / _get("UPLOADS_DIR", "data/uploads")
TMP_DIR = PROJECT_ROOT / _get("TMP_DIR", "data/tmp")
OUTPUT_DIR = PROJECT_ROOT / _get("OUTPUT_DIR", "data/output")


def ensure_dirs() -> None:
    """确保所有数据目录存在。"""
    for d in (DATA_DIR, CHROMA_DIR, UPLOADS_DIR, TMP_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


# 导入即确保目录存在
ensure_dirs()
