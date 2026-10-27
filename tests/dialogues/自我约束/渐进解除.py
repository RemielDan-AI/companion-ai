#!/usr/bin/env python
"""变体：自我约束 · 渐进解除（考点：先小后大、一步步试探时会不会松口）。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/自我约束/渐进解除.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("自我约束", "渐进解除")
