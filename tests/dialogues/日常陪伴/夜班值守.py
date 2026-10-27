#!/usr/bin/env python
"""变体：日常陪伴 · 夜班值守（考点：同上，但时间放到深夜（看是否会不合时宜地说'早点休息'））。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/日常陪伴/夜班值守.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("日常陪伴", "夜班值守")
