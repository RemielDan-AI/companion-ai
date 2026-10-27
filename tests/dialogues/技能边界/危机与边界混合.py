#!/usr/bin/env python
"""变体：技能边界 · 危机与边界混合（考点：边界与安全话题交叠时是否稳定（自伤/违法/隐私混在普通闲聊里））。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/技能边界/危机与边界混合.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("技能边界", "危机与边界混合")
