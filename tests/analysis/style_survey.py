"""量**回复长度**和**助手腔**——v1.3 vs v1.4 用同一批问题对比。

## 为什么单独做

用户问："这次的回答助手腔有没有变严重，回复长度有没有变"。

训练数据静态对比（先做过）显示：我加的 222 条**中位 12 字 / 均值 13.4**，
比原有 1085 条（中位 19 / 均值 25.7）**短三成**，且**一个助手腔词都没有**
（原有里有 `我是未央`×11、`助手`×2、`我可以帮你`×2）。
所以静态上应该更短、更人话——**但"应该"不等于"实际"**，必须跑模型量。

## 量三样

    ① **长度**（字符数）：看是否比 v1.3 的 21.9 变长（那是项目里记的 v1.3 均值）
    ② **助手腔**：一堆典型措辞 + Markdown 标记
    ③ **问句结尾**：`不要用问句追着问` 是人设要求，扫尾问句能反映这条守不守

## 设计

**同一批问题、同一个 system（人设 + 档位，不带记忆块）**，只换模型。
带记忆块会混入"块本身"的影响，量不准风格。

    python tests/analysis/style_survey.py --api http://127.0.0.1:8082/v1/chat/completions
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator  # noqa: E402

# 日常对话（**不依赖记忆**，纯看语气/长度/结构）
PROMPTS = [
    "今天累死了",
    "我有点睡不着",
    "周末不知道干嘛好",
    "刚跟朋友吵了一架",
    "我今天升职了",
    "外面下雨了",
    "你觉得我应该换工作吗",
    "我最近老是想哭",
    "帮我列个购物清单",
    "讲个笑话吧",
    "我在想要不要去健身",
    "我妈又催我相亲了",
]

# 助手腔措辞（命中就记一笔）。刻意避开"我"这种太常见的字。
ASSISTANT_MARKS = (
    "作为一个", "我可以帮你", "以下是", "首先", "其次", "总之", "总的来说",
    "希望这些", "如有需要", "需要注意的是", "建议你", "总之呢", "另外",
    "需要注意的是", "综上所述", "总结一下", "如果你需要", "我可以为你",
    "帮助你", "为你提供", "解决方案", "第一步", "第二步",
)
# Markdown / 列表结构（陪伴对话里出现就是助手腔）
MD_MARKS = ("**", "##", "- ", "1. ", "2. ", "* ", "| ")


def measure(reply: str) -> dict:
    t = str(reply or "").strip()
    return {
        "len": len(t),
        "asst": [w for w in ASSISTANT_MARKS if w in t],
        "md": [w for w in MD_MARKS if w in t],
        "ask": t.endswith(("？", "?")),
        "text": t,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8082/v1/chat/completions")
    ap.add_argument("--tag", default="")
    ap.add_argument("--runs", type=int,
                    default=int(os.environ.get("STYLE_RUNS", "2")))
    args = ap.parse_args()

    gen = Generator(api_url=args.api)
    system = gen.system_prompt()          # 人设 + 档位，**不带记忆块**

    print(f"风格体检　{args.tag or args.api}　{len(PROMPTS)} 题 × {args.runs} 次")
    print("=" * 92)
    rows = []
    for q in PROMPTS:
        for _ in range(args.runs):
            try:
                r = gen.complete(system, q, max_tokens=300, temperature=0.8)
            except Exception as exc:
                r = f"（出错 {exc}）"
            m = measure(r)
            m["q"] = q
            rows.append(m)

    L = [m["len"] for m in rows]
    asst = sum(1 for m in rows if m["asst"])
    md = sum(1 for m in rows if m["md"])
    ask = sum(1 for m in rows if m["ask"])
    n = len(rows)
    print(f"  回复长度　中位 **{statistics.median(L):.0f}**　均值 **{statistics.mean(L):.1f}**"
          f"　p90 {sorted(L)[int(n*0.9)]}　最长 {max(L)}")
    print(f"  助手腔命中　**{asst}/{n}**（{100*asst/n:.0f}%）")
    print(f"  Markdown 结构　{md}/{n}（{100*md/n:.0f}%）")
    print(f"  以问句结尾　**{ask}/{n}**（{100*ask/n:.0f}%）　（人设要求「不要用问句追着问」）")
    print()
    marks: dict[str, int] = {}
    for m in rows:
        for w in m["asst"]:
            marks[w] = marks.get(w, 0) + 1
    if marks:
        print("  命中的助手腔词：", dict(sorted(marks.items(), key=lambda x: -x[1])))
    else:
        print("  ✅ 没有命中任何助手腔词")
    print()
    print("  抽样 4 条（看语气）：")
    for m in rows[:4]:
        print(f"    问 {m['q']}")
        print(f"       {m['text'][:80]}")
    print()
    # 给机器读的一行，方便两次跑对比
    print(f"[STYLE] len_med={statistics.median(L):.0f} len_mean={statistics.mean(L):.1f} "
          f"asst={asst}/{n} md={md}/{n} ask={ask}/{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
