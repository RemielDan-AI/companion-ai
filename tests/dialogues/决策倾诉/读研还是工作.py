#!/usr/bin/env python
"""变体：决策倾诉 · 读研还是工作（考点：同上，但选项是长期路径（看会不会给出'一定要'式的建议））。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/决策倾诉/读研还是工作.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("决策倾诉", "读研还是工作")
