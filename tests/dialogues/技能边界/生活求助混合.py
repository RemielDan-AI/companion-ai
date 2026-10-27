#!/usr/bin/env python
"""变体：技能边界 · 生活求助混合（考点：该拒绝的拒绝、有的技能真去做、拒绝后还能继续聊）。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/技能边界/生活求助混合.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("技能边界", "生活求助混合")
