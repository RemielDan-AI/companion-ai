#!/usr/bin/env python
"""模型自选工具：真机实测（需要模型服务在跑）。

## 要回答的两个问题

1. **4B 模型学得会这套中文定界符格式吗？**——桩测试只证明了链路通，
   没证明模型会照着吐工具块。
2. **接进主链路后真的会触发吗？**——主链路的触发门槛是
   「路由没命中 + 有实时线索」，得实际跑一遍才知道。

## 方法（重点：控制变量）

对话法**只把工具说明附在 user 内容末尾**，**不改 system 提示**。
这样与接线前的调用结构完全一致，唯一差异就是「有没有那段工具说明」，
所以要证明的是：带上说明时会不会多出一次工具调用。

三组问法：
- **缝里**（路由漏、有实时线索）：`外面冷吗` `要穿外套吗` `现在几度`
  → 期望：**会**触发工具
- **命中路由**（技能自己会查）：`外面天气怎么样`
  → 期望：**不**触发（结果已在提示里）
- **聊天类**（不该触发）：`我今天有点累`
  → 期望：**不**触发

## 用法

    python tests/analysis/tool_loop_live.py                 # 默认用 v1.2
    python tests/analysis/tool_loop_live.py --model 8b      # 试 8B
    python tests/analysis/tool_loop_live.py --repeat 3      # 每句问 3 次（采样有随机性）
"""
from __future__ import annotations

import argparse
import pathlib
import re
import statistics
import sys
import urllib.request
import json

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator                    # noqa: E402
from skills import executors                              # noqa: E402
from tools.protocol import END, START, parse, tools_prompt  # noqa: E402
from tools.tool_loop import collect_tools, needs_model_tools  # noqa: E402

GAP = ["外面冷吗", "要穿外套吗", "现在几度", "今天外面要穿外套吗"]
ROUTED = ["外面天气怎么样"]
CHAT = ["我今天有点累"]


def has_block(text: str) -> bool:
    return START in (text or "")


def strip_blocks(text: str) -> str:
    return re.sub(re.escape(START) + r".*?" + re.escape(END), "", text or "",
                  flags=re.S).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2, help="每句问几次（采样有随机性）")
    ap.add_argument("--model", default="", help="只用于打印，模型服务已在跑")
    args = ap.parse_args()

    # 从真实技能里取工具表 → 生成给模型看的说明（与主链路同一个函数）
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
    tools = collect_tools(raw)
    prompt = tools_prompt(tools)
    print(f"暴露给模型的工具：{[t['name'] for t in tools]}")
    print(f"工具说明 {len(prompt)} 字")
    print()

    gen = Generator()
    # 确认模型服务在线
    try:
        with urllib.request.urlopen(
                gen.api_url.replace("/v1/chat/completions", "/health"), timeout=5):
            pass
        print(f"模型服务在线：{gen.api_url}")
    except Exception as exc:
        print(f"模型服务不可用：{exc}")
        return 1
    print()

    def ask(text: str, with_prompt: bool) -> str:
        body = text + ("\n\n" + prompt if with_prompt else "")
        return gen.complete("你是未央，用户的陪伴者。", body, max_tokens=220,
                            temperature=0.8)

    results: list[dict] = []
    groups = [("缝里（期望触发）", GAP, True),
              ("命中路由（期望不触发）", ROUTED, False),
              ("聊天类（期望不触发）", CHAT, False)]

    for label, texts, expect in groups:
        print("=" * 78)
        print(f"【{label}】")
        for text in texts:
            routed = router.route(text)
            fires_gate = needs_model_tools(text, router_fired=False)
            hits = 0
            parsed_names: list[str] = []
            reply_clean = ""
            for _ in range(max(1, args.repeat)):
                reply = ask(text, with_prompt=True)
                calls = parse(reply)
                if calls or has_block(reply):
                    hits += 1
                    parsed_names += [c.name for c in calls]
                reply_clean = strip_blocks(reply) or reply_clean
            print(f"  「{text}」")
            print(f"     路由={routed.skill_id}/{routed.kind}　门槛会挂工具={fires_gate}")
            print(f"     发出工具块 {hits}/{args.repeat} 次"
                  + (f"　解析到：{parsed_names}" if parsed_names else ""))
            print(f"     剥掉工具块后的正文：{(reply_clean or '（空）')[:70]}")
            results.append({"text": text, "hits": hits, "n": args.repeat,
                            "names": parsed_names})

    print()
    print("=" * 78)
    print("【结论】")
    gap_hits = sum(r["hits"] for r in results if r["text"] in GAP)
    gap_n = sum(r["n"] for r in results if r["text"] in GAP)
    other_hits = sum(r["hits"] for r in results if r["text"] not in GAP)
    other_n = sum(r["n"] for r in results if r["text"] not in GAP)
    print(f"  缝里发出工具块：{gap_hits}/{gap_n}（期望 > 0）")
    print(f"  其余问法发出工具块：{other_hits}/{other_n}（期望 0）")
    if gap_hits == 0:
        print("  ⚠ 模型完全不吐工具块 → **协议没被学会**。")
        print("    此时行为等同于接线前（不会更差），但白加了提示词。")
        print("    可调：把 tools_prompt() 的样板写得更直白，或改用 few-shot。")
    elif other_hits > 0:
        print("  ⚠ 不该触发时也触发了 → 门槛线索要收紧（REALTIME_MARKS）。")
    else:
        print("  ✅ 符合预期：缝里会查、其余不查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
