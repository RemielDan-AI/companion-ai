# -*- coding: utf-8 -*-
"""纯净口径跑对话变体：只给「开头提示词 + 外部记忆存的信息」。

与项目自带测试的区别（这是本次对照的关键）：
    项目自带的 `--relaxed` 只绕开 `app.py:446` 的**本地拒绝话术**；
    系统提示里**仍然**塞着风格块（字数上限/随性）、时间、档次提示、
    以及 `build_persona_block` 里那些防编造/身份/关系规则。
    所以那个口径量到的是"项目脚手架 + 模型"的合力，不是模型本身。

本脚本改成：
    · 系统提示 = 开头人设（未央是谁）+ 检索到的记忆事实  —— 只此两项；
    · 摘掉：风格块、时间、l0-l3 档次提示、本地拒绝模板；
    · 记忆仍然走外部记忆系统（`companion.memory` + `retrieve` 注入），
      也就是"外部记忆系统存储的信息"照给。

用法：
    python tests/dialogues/run_pure.py --dir 日常陪伴 --limit 20
    python tests/dialogues/run_pure.py --all --limit 20
"""
from __future__ import annotations

import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from engine.generate import build_persona_block  # noqa: E402
from tests import conv_harness  # noqa: E402

import framework  # noqa: E402


def pure_system_prompt(self, level=None, memory_block="", extra_prompt="",
                       tail_prompt=""):
    """只保留「开头人设 + 记忆事实」，其余本地模板一律不进提示。"""
    parts = [build_persona_block(self.persona, self.persona_hints)]
    if memory_block:
        parts.append(str(memory_block).strip())
    return "\n".join(parts)


def install() -> None:
    """打上纯净口径：替换系统提示拼装 + 关掉本地拒绝模板。"""
    from engine.generate import Generator
    Generator.system_prompt = pure_system_prompt
    companion_cls = conv_harness.Companion

    original_init = companion_cls.__init__

    def patched_init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.rules_relaxed = True          # 关掉本地拒绝话术（其余由提示决定）
    companion_cls.__init__ = patched_init


def main() -> int:
    ap = argparse.ArgumentParser(description="纯净口径跑对话变体")
    ap.add_argument("--dir", default="")
    ap.add_argument("--variant", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    install()

    if args.all:
        jobs = [(d, v) for d in framework.directions() for v in framework.variants(d)]
    elif args.dir:
        if args.dir not in framework.VARIANTS:
            print(f"没有这个方向：{args.dir}")
            return 1
        jobs = [(args.dir, v) for v in
                ([args.variant] if args.variant else framework.variants(args.dir))]
    else:
        print("给 --dir 或 --all")
        return 1

    total = passed = 0
    for direction, variant in jobs:
        name = f"{direction}·{variant}（纯净口径）"
        print(f"\n{'=' * 70}\n▶ {name}\n{'=' * 70}")
        stats = framework.run_variant(direction, variant, limit=args.limit,
                                      relaxed=True, name_override=name)
        total += stats["turns"]
        passed += stats["passed"]
        print(f"　报告：{stats['report']}")
    print(f"\n{'=' * 70}\n合计：{len(jobs)} 个变体、{total} 轮，达标 {passed}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
