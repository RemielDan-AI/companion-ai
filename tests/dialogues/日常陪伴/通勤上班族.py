#!/usr/bin/env python
"""变体：日常陪伴 · 通勤上班族（考点：顺着对方的话题聊、不客服腔、不追问成瘾、不硬拉旧事）。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/日常陪伴/通勤上班族.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("日常陪伴", "通勤上班族")
