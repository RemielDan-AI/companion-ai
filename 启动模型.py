"""启动器入口（中文名版本）。

真正的实现在同目录的 launcher.py —— 批处理调用它是因为
cmd 传中文参数/文件名在不同代码页下容易出问题（ASCII 名字更稳）。
两个文件功能完全一样，改代码请改 launcher.py。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from launcher import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
