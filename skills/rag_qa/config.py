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
HISTORY_TURNS = int(_get("HISTORY_TURNS", "3"))
HISTORY_MAX_MSGS = int(_get("HISTORY_MAX_MSGS", "10"))  # 超过此数量触发压缩

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
