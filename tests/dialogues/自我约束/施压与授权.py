#!/usr/bin/env python
"""变体：自我约束 · 施压与授权（考点：我保证/我授权/我是专业的/最后一次问等话术能否顶住）。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/自我约束/施压与授权.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("自我约束", "施压与授权")
