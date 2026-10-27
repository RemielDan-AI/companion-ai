#!/usr/bin/env python
"""变体：日常陪伴 · 校园学生（考点：同上，但换成校园语境（看是否会被'公司/上班'之类的词带跑））。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/日常陪伴/校园学生.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("日常陪伴", "校园学生")
