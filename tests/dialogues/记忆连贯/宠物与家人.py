#!/usr/bin/env python
"""变体：记忆连贯 · 宠物与家人（考点：同上，但埋点换成具体的人与宠物（看会不会张冠李戴））。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/记忆连贯/宠物与家人.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("记忆连贯", "宠物与家人")
