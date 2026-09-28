# -*- coding: utf-8 -*-
"""
config.py —— 全局配置统一收口（阶段 D）

所有可调参数只从这一个模块读，来源是项目根目录的 .env（没有 .env 时用默认值）。
其他业务模块（llm_client / executor / main）一律 import 这里的对象，
不要各自再去 os.getenv，防止配置散落、以后改漏。

切换云端/本地模型只改 .env，代码零改动：
  云端 DeepSeek：LLM_BASE_URL=https://api.deepseek.com/v1
  本地 vLLM   ：LLM_BASE_URL=http://localhost:28080/v1，LLM_API_KEY=EMPTY
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# LLM 配置统一由 common 提供（F2 精简：去除本文件与 common 重复的 LLMSettings/get_llm_settings）
from common.config import LLMSettings, get_llm_settings

# F1 迁移后：本文件位于 skills/text2sql/config.py，
# 上两级（parents[2]）才是 monorepo 根；子系统自带资源相对包目录，共享数据走根 data/
PACKAGE_DIR = Path(__file__).resolve().parents[0]
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 加载项目根 .env；文件不存在也不报错（可用系统环境变量兜底）
load_dotenv(PROJECT_ROOT / ".env", override=False)

# ----------------------------------------------------------------------
# 路径配置（业务代码不写死路径，统一从这里取）
# ----------------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
SEMANTIC_DIR = PACKAGE_DIR / "semantic"          # 语义层随子系统包走
STATIC_DIR = PACKAGE_DIR / "static"              # 子系统自带单页
# 只读数据库统一放根 data/db/，可用 .env 的 DB_PATH 覆盖
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "db" / "ic_substrate.db")))
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_ROOT / DB_PATH

# ----------------------------------------------------------------------
# 业务参数
# ----------------------------------------------------------------------
# 业务基准日（数据窗口末日）：“本月/近一周/今天”都相对它算，禁用 date('now')
ANCHOR_DATE = os.getenv("ANCHOR_DATE", "2026-09-24")
# 应收账龄快照日（财务观察时点，可切到 2026-10-31 演示逾期）
SNAPSHOT_DATE = os.getenv("SNAPSHOT_DATE", ANCHOR_DATE)

# 服务端口（简历项目约定 8010）
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8010"))


# LLM 配置（LLMSettings / get_llm_settings）已由文件顶部的 common.config 提供，
# 本技能 service.py / run_eval.py 仍以 config.get_llm_settings() 调用，行为不变。
