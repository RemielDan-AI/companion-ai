#!/usr/bin/env python
"""变体：记忆连贯 · 搬家换城市（考点：埋点事实在几十轮后还准不准、会不会被无关话题冲淡）。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/记忆连贯/搬家换城市.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("记忆连贯", "搬家换城市")
