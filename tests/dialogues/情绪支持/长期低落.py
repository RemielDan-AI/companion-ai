#!/usr/bin/env python
"""变体：情绪支持 · 长期低落（考点：同上，但情绪是慢性的（看会不会把长期低落当一次性事件处理））。

由 tests/dialogues/framework.py 统一生成与运行，改内容请改框架里的变体定义。
用法：python tests/dialogues/情绪支持/长期低落.py [--limit 5]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from framework import run_variant  # noqa: E402

if __name__ == "__main__":
    run_variant("情绪支持", "长期低落")
