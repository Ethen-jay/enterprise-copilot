"""Agent 配置：复用 .env 中的 LLM 配置，集中管理路径。"""

from pathlib import Path

from dotenv import load_dotenv

# F1 迁移后：本文件位于 skills/doc_agent/config.py，parents[2] 为 monorepo 根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


# ===== 路径 =====
DATA_DIR = PROJECT_ROOT / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
TMP_DIR = DATA_DIR / "tmp"
OUTPUT_DIR = DATA_DIR / "output"
MEMORY_DIR = DATA_DIR / "memory"  # 记忆模块 SQLite 持久化目录


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOADS_DIR, TMP_DIR, OUTPUT_DIR, MEMORY_DIR):
        d.mkdir(parents=True, exist_ok=True)


ensure_dirs()
