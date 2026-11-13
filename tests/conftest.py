"""把项目根目录加入 sys.path，让 tests 能直接 import 平铺模块。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
