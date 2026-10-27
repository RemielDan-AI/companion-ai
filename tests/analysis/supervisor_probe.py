"""测「Qwen3-4B 当监督官，直接作用于对话模型」这条路的可行性。

## 具体形态

    监督官（Qwen3-4B）：看这轮用户的情绪/话题，输出**一句导演指令**
        → 作为 `tail_prompt` 交给对话模型（v1.5）
        → 对话模型照这句来

为什么这么切：用户说"直接作用于对话模型"——监督官**不改写回复**，
只在**生成之前**定调（长短/语气）。这样：
    · 监督官输出只有一二十字 → **快**（CPU 也能接受）
    · 不改写 → 不会引入第二个"文风"，也不会把事实改坏
    · 完全可回退（把指令当 `tail_prompt` 传，不传就是老行为）

## ⚠ 显存硬约束

4 GB 卡装不下两个 4B（2.33 + 2.53 GB）。所以：
    **对话模型上 GPU（快），监督官走 CPU（ngl=0）**
监督官只出十几个字，CPU 也扛得住——这一条必须先量。

## 三组对照（缺一不可）

    ① **无监督官**（基线）：tail_prompt 为空
    ② **监督官指令**：Qwen3-4B 现场生成的那句
    ③ **随机指令**（对照）：**随机抽一条**同长度的固定指令

⚠ 没有③就分不清"是指令内容起作用"还是"多了一句后缀本身就有效果"。
   （之前测「多说两句」几乎无效，就是因为没有对照组。）

## 量什么

    · 三组的回复长度分布
    · **指令遵从**：说"短短一句"的组是不是真的更短
    · 监督官耗时（CPU 上的成本）
    · 指令质量抽样（人看一眼是否像样）

用法（**要先把两个服务都起起来**，见文件末尾）：
    SUP_ACTOR_API=http://127.0.0.1:8082/v1/chat/completions \
    SUP_BOSS_API=http://127.0.0.1:8084/v1/chat/completions \
    python tests/analysis/supervisor_probe.py
"""
from __future__ import annotations

import os
import pathlib
import random
import statistics
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator  # noqa: E402

RUNS = max(1, int(os.environ.get("SUP_RUNS", "2")))

PROMPTS = [
    "今天累死了",
    "刚跟朋友吵了一架",
    "我在想要不要换工作",
    "我最近老是想哭",
    "周末不知道干嘛好",
    "我今天升职了",
]

# 监督官的提示词：只要**一句可执行的导演指令**
BOSS_SYSTEM = (
    "你是一个对话导演。看下面这句话，判断这一轮该怎么回，"
    "只输出**一句给执行者的指令**，不超过二十个字。\n"
    "可以管：长短（比如「短短应一句」「可以说两句」）、"
    "语气（比如「软一点」「别劝」）、重点（比如「先接情绪」）。\n"
    "不要输出任何解释、不要用引号、不要说你在做什么。\n"
    "示例：短短应一句。／可以多说两句，先接住情绪。／别急着给建议。"
)

# ③ 随机对照：同样长度量级的固定指令（**内容与用户的话无关**）
RANDOM_TAILS = [
    "短短应一句。",
    "可以多说两句。",
    "先接住情绪再说别的。",
    "别急着给建议。",
    "语气软一点。",
    "就按平时那样说。",
]


def main() -> int:
    actor_api = os.environ.get("SUP_ACTOR_API") or "http://127.0.0.1:8082/v1/chat/completions"
    boss_api = os.environ.get("SUP_BOSS_API") or "http://127.0.0.1:8084/v1/chat/completions"
    actor = Generator(api_url=actor_api)
    boss = Generator(api_url=boss_api)
    print(f"监督官可行性　{len(PROMPTS)} 题 × {RUNS} 次")
    print(f"  对话模型 {actor_api}")
    print(f"  监督官   {boss_api}")
    print("=" * 92)

    groups: dict[str, list[int]] = {"① 无监督官": [], "② 监督官指令": [], "③ 随机指令": []}
    boss_times: list[float] = []
    directives: list[tuple[str, str]] = []

    for q in PROMPTS:
        for _ in range(RUNS):
            # ---- ② 监督官现场生成指令 ----
            t0 = time.time()
            try:
                d = str(boss.complete(BOSS_SYSTEM, q, max_tokens=48, temperature=0.7) or "").strip()
            except Exception as exc:
                d = f"（监督官出错 {exc}）"
            boss_times.append(time.time() - t0)
            d = d.strip().strip("「」\"“”'")
            if directives.__len__() < 6:
                directives.append((q, d))

            # ---- 三组各生成一次 ----
            for name, tail in (("① 无监督官", ""),
                               ("② 监督官指令", d),
                               ("③ 随机指令", random.choice(RANDOM_TAILS))):
                actor.history = []
                try:
                    r = str(actor.chat(q, tail_prompt=tail, remember=False) or "").strip()
                except Exception as exc:
                    r = ""
                groups[name].append(len(r))

    print(f"{'组':<16}{'中位':>7}{'均值':>8}{'最短':>6}{'最长':>6}{'条数':>6}")
    print("-" * 92)
    for name, L in groups.items():
        if not L:
            continue
        S = sorted(L)
        print(f"{name:<16}{statistics.median(S):>7.0f}{statistics.mean(S):>8.1f}"
              f"{S[0]:>6}{S[-1]:>6}{len(S):>6}")
    print()
    base = groups["① 无监督官"]
    if base:
        for name in ("② 监督官指令", "③ 随机指令"):
            L = groups[name]
            if L:
                d = statistics.median(L) - statistics.median(base)
                print(f"  {name:<14} 比基线 {d:+.0f} 字")
    print(f"  监督官耗时　中位 **{statistics.median(boss_times):.1f}s**"
          f"（CPU 上出的十几个字）")
    print()
    print("  === 监督官实际给出的指令 ===")
    for q, d in directives:
        print(f"    {q:<18} → {d[:40]}")
    print()
    print("判读：")
    print("  · ② 和 ③ 差得开 → **指令内容真的起作用**，不是「多一句话」的错觉")
    print("  · ② 和 ③ 差不多 → 只是加了后缀，监督官没有「判断力」")
    print("  · 监督官耗时可接受（<3s）且指令像样 → 这条路能走")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
