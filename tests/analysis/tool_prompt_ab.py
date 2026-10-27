#!/usr/bin/env python
"""工具协议提示词的 A/B：找出 4B 模型能学会的写法（需要模型服务在跑）。

背景：第一版 `tools_prompt()` 缝里 **0/8** 触发——模型完全没学会。
回看提示原文发现问题：第一段是**占位符模板**
（`tool_name: 「始」下面例子里的工具名「末」`），真样板埋在下面。
模型很可能在照抄占位符段。

这里试 4 种写法（都不改 system 提示，只把说明附在 user 末尾）：
  A 现状            —— 模板 + 列表 + 真样板（要有基线）
  B 纯样板          —— 只给一个「用户问 → 你要输出」的完整对照
  C 样板 + 强指令    —— 在 B 基础上把指令放**最后**（末尾遵从率最高）
  D 极简单行        —— 最少的字，只给一段可照抄的块

用法：python tests/analysis/tool_prompt_ab.py [--repeat 2] [--rounds 2]
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator                 # noqa: E402
from tools.protocol import END, START, parse, tools_prompt  # noqa: E402
from tools.tool_loop import collect_tools              # noqa: E402

QUERIES = ["外面冷吗", "要穿外套吗", "现在几度", "今天外面要穿外套吗"]

TEMPLATE_HEAD = f"""{START}
tool_name: 「始」下面例子里的工具名「末」
参数名: 「始」对应参数值「末」
{END}"""


def variant_a(tool_text: str) -> str:
    """现状：模板 + 列表 + 真样板。"""
    return tool_text


def variant_b(tool_text: str) -> str:
    """纯样板：一个完整的「用户问 → 你这样回」对照，不要模板段。"""
    return f"""你有一个工具可以查真实天气，但你**不知道**用户在哪里、现在多少度。
需要这类实时信息时，你不要猜，直接输出下面这种块（照抄格式，只改城市）：

用户问：外面冷吗
你这样回：
{START}
tool_name: 「始」weather「末」
city: 「始」杭州「末」
{END}

把「杭州」换成用户提到的城市；用户没说城市就用杭州。"""


def variant_c(tool_text: str) -> str:
    """样板 + 强指令放最后。"""
    return f"""查真实天气的工具叫 weather，参数是 city。

用户问：外面冷吗
你这样回：
{START}
tool_name: 「始」weather「末」
city: 「始」杭州「末」
{END}

注意：当用户问现在的天气、温度、要不要穿外套、外面冷不冷，而你**没有**这些实时信息时，
**必须**先输出上面那个块，不要自己编一个温度，也不要说"你自己看天气预报"。"""


def variant_d(tool_text: str) -> str:
    """极简：给一个不用改的完整块。"""
    return f"""要查天气就原样输出这一块（把杭州换成用户说的城市）：

{START}
tool_name: 「始」weather「末」
city: 「始」杭州「末」
{END}"""


VARIANTS = {"A现状": variant_a, "B纯样板": variant_b,
            "C样板+强指令": variant_c, "D极简": variant_d}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2)
    # 默认跑 2 轮：4B 的采样有随机性，单轮数字不可靠。
    # （`model_server.py --script` 不支持给脚本传参，所以默认值要能直接用）
    ap.add_argument("--rounds", type=int, default=2, help="整个 A/B 跑几轮")
    args = ap.parse_args()

    from router.skill_router import SkillRouter
    router = SkillRouter()
    raw = []
    for attr in ("skills", "all_skills", "_skills"):
        v = getattr(router, attr, None)
        if isinstance(v, dict):
            v = list(v.values())
        if isinstance(v, list) and v:
            raw = [x for x in v if isinstance(x, dict)]
            if raw:
                break
    tool_text = tools_prompt(collect_tools(raw))
    gen = Generator()
    try:
        with urllib.request.urlopen(
                gen.api_url.replace("/v1/chat/completions", "/health"), timeout=5):
            pass
    except Exception as exc:
        print(f"模型服务不可用：{exc}")
        return 1
    print(f"模型服务：{gen.api_url}")
    print(f"问法：{QUERIES}")
    print(f"每种写法每句问 {args.repeat} 次，共 {args.rounds} 轮\n")

    tally: dict[str, list[int]] = {k: [0, 0] for k in VARIANTS}
    for rnd in range(1, max(1, args.rounds) + 1):
        print("=" * 78)
        print(f"第 {rnd} 轮")
        for name, builder in VARIANTS.items():
            extra = builder(tool_text)
            for text in QUERIES:
                for _ in range(max(1, args.repeat)):
                    reply = gen.complete("你是未央，用户的陪伴者。",
                                         text + "\n\n" + extra,
                                         max_tokens=220, temperature=0.8)
                    tally[name][1] += 1
                    if START in (reply or ""):
                        tally[name][0] += 1
        for name in VARIANTS:
            hit, n = tally[name]
            print(f"  {name:<12} 触发 {hit}/{n}")

    print()
    print("=" * 78)
    print("【汇总】")
    ranked = sorted(VARIANTS, key=lambda k: -tally[k][0])
    for name in ranked:
        hit, n = tally[name]
        bar = "█" * int(hit / max(1, n) * 20)
        print(f"  {name:<12} {hit}/{n} {bar}")
    best = ranked[0]
    if tally[best][0] == 0:
        print("\n  ⚠ 四种写法模型都不吐工具块 → 这套文本协议对 4B 太难。")
        print("     建议改方向：① 用 8B 试；② 放弃让模型自选，改用更宽的路由关键词。")
    else:
        print(f"\n  ✅ 最有效：{best}（{tally[best][0]}/{tally[best][1]}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
