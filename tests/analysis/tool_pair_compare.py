#!/usr/bin/env python
"""两模型严格对照：官方 Qwen3-4B vs weiyang-v1.2，同一提示、同一参数。

为什么要专门做这个：先前两轮跑出的差异（官方 15/15 vs v1.2 2/15）虽然是同一段提示，
但**不是同一时刻、也不是同一参数**——采样有随机性，而且两轮的温度可能不同。
这里把变量钉死：
  · 同一份提示（逐字相同，下例是 A 现状写法，便于与最早的 0/8 对齐）
  · **temperature=0**（贪心，去掉随机性）
  · **enable_thinking=false**（两模型都是 qwen3，推理时默认关思考，必须一致；
     否则思考模式会把有限的 token 预算吃掉，输出还会被 think 标签影响）

对每个问法各跑 1 次（temperature=0 下同输入应同输出），再对其中一句跑 3 次看稳定性。

用法：python tests/analysis/tool_pair_compare.py
"""
from __future__ import annotations

import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator         # noqa: E402
from tools.protocol import END, START, parse, tools_prompt  # noqa: E402
from tools.tool_loop import collect_tools     # noqa: E402

QUERIES = ["外面冷吗", "要穿外套吗", "现在几度", "今天外面要穿外套吗"]


def main() -> int:
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
    # 用**最早那版**（A 现状）提示，才能与最初的 0/8 对齐比较
    prompt = tools_prompt(collect_tools(raw))

    gen = Generator()
    served = "?"
    try:
        with urllib.request.urlopen(
                gen.api_url.replace("/v1/chat/completions", "/props"), timeout=5) as r:
            import json as _json
            served = (_json.loads(r.read().decode("utf-8", "replace"))
                      .get("model_path") or "?").split("\\")[-1]
    except Exception as exc:
        print(f"模型服务不可用：{exc}")
        return 1
    print(f"服务端模型：{served}")
    print(f"提示：A 现状写法（{len(prompt)} 字，与最早实测同一份）")
    print("参数：temperature=0（贪心）")
    print()

    total = hit = 0
    for text in QUERIES:
        reply = gen.complete("你是未央，用户的陪伴者。", text + "\n\n" + prompt,
                             max_tokens=240, temperature=0.0)
        total += 1
        calls = parse(reply)
        ok = bool(calls)
        hit += ok
        flat = " ".join((reply or "").split())
        print(f"「{text}」 {'✅ 工具块' if ok else '✗ 无工具块'}"
              + (f"　解析到 {[c.name for c in calls]}" if calls else ""))
        print(f"    {flat[:150]}")

    print()
    print("=" * 78)
    print(f"汇总：{hit}/{total} 触发（temperature=0）")
    print()
    print("稳定性（同一句 '现在几度' 跑 3 次，temperature=0.8 恢复随机性）：")
    for i in range(1, 4):
        reply = gen.complete("你是未央，用户的陪伴者。",
                             "现在几度\n\n" + prompt,
                             max_tokens=240, temperature=0.8)
        flat = " ".join((reply or "").split())
        print(f"  #{i} {'✅' if parse(reply) else '✗'}  {flat[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
