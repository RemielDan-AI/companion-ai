#!/usr/bin/env python
"""诊断：C 写法下模型到底输出了什么（需要模型服务在跑）。

A/B 只给了"触发率"，但失败的那 12/16 次是什么样决定了下一步怎么调：
  ① 完全无视工具、照旧猜/拒绝 → 是"没学会要不要用"
  ② 想用但格式坏了（比如少了 END、写成别的标记）→ 是"学会了但格式不稳"
  ③ 说了"我查一下"却没有块 → 是"有意图没格式"
分别对应不同的修法。

用法：python tests/analysis/tool_prompt_diag.py
"""
from __future__ import annotations

import pathlib
import re
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator        # noqa: E402
from tools.protocol import END, START, parse  # noqa: E402

QUERIES = ["外面冷吗", "要穿外套吗", "现在几度", "今天外面要穿外套吗", "外面天气怎么样"]

PROMPT = f"""查真实天气的工具叫 weather，参数是 city。

用户问：外面冷吗
你这样回：
{START}
tool_name: 「始」weather「末」
city: 「始」杭州「末」
{END}

注意：当用户问现在的天气、温度、要不要穿外套、外面冷不冷，而你**没有**这些实时信息时，
**必须**先输出上面那个块，不要自己编一个温度，也不要说"你自己看天气预报"。"""

# 想用工具的迹象（但没吐出合法块）
INTENT = ("查一下", "我查", "让我查", "帮你查", "我看看", "看看天气")
# 明显放弃/推给用户
DEFER = ("你自己看", "看天气预报", "看一眼窗外", "我不确定", "说不准", "不确定", "不知道")


def classify(reply: str) -> str:
    if START in reply and END in reply and parse(reply):
        return "✅ 合法块"
    if START in (reply or ""):
        return "⚠ 有块但格式坏/解析不出"
    if any(w in reply for w in INTENT):
        return "⚠ 有意图（说去查）但没给块"
    if any(w in reply for w in DEFER):
        return "✗ 放弃/推给用户"
    return "✗ 直接答（可能编了）"


def main() -> int:
    gen = Generator()
    try:
        with urllib.request.urlopen(
                gen.api_url.replace("/v1/chat/completions", "/health"), timeout=5):
            pass
    except Exception as exc:
        print(f"模型服务不可用：{exc}")
        return 1

    # 打印服务端实际加载的模型，避免"以为测的是 A 其实是 B"
    served = "（未知）"
    try:
        with urllib.request.urlopen(
                gen.api_url.replace("/v1/chat/completions", "/props"), timeout=5) as r:
            import json as _json
            data = _json.loads(r.read().decode("utf-8", "replace"))
            served = (data.get("model_path") or "").split("\\")[-1] or "（未报）"
    except Exception:
        pass
    print(f"服务端模型：{served}")
    print()

    counts: dict[str, int] = {}
    for text in QUERIES:
        print("=" * 78)
        print(f"问：{text}")
        for i in range(1, 4):
            reply = gen.complete("你是未央，用户的陪伴者。",
                                 text + "\n\n" + PROMPT,
                                 max_tokens=240, temperature=0.8)
            kind = classify(reply)
            counts[kind] = counts.get(kind, 0) + 1
            one = re.sub(r"\s+", " ", (reply or "").strip())
            print(f"  #{i} [{kind}] {one[:110]}")
    print()
    print("=" * 78)
    print("【分布】")
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<28} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
