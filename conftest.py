# -*- coding: utf-8 -*-
"""pytest 引导：把 monorepo 根加入 sys.path，使测试能 `import skills.*`。

测试文件内的数据路径统一用 ROOT 常量（不依赖运行时 cwd）拼绝对路径。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))