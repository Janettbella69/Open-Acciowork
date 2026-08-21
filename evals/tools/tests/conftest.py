"""评测工具测试自带路径配置：只挂 evals/tools，不依赖主系统包。"""

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
