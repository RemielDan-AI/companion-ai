"""端到端验「主回复 → 补充 → 回声判定 → 决定」这一整套（用户的方案）。

## 方案

    ① 主回复（正常生成）
    ② 补充（另一次生成，问"还有什么想补充的"）
    ③ **回声判定：实义二元组重合 ≥ 阈值 → 判为重复**
          · 重复 → **丢掉补充**（这一轮就是短回复）
          · 新   → **接上去**（这一轮是长回复）
    ④ 补充还要过一遍 `strip_claims`（防"你上次也…"那种编造）

预期：**约一半轮次短、一半轮次长 → 时短时长**，且**回声一条都不上线**。

## 为什么判定用闸值不用模型

上一个探针（`echo_judge_probe.py`）测过：14 对标注上，
    4B 监督官  准确 86% / 精确 100% / 召回 71%
    二元组阈值 0.3  **准确 100%** / 精确 100% / 召回 100%
**模型没赢，还贵**。这里用阈值。

## 量什么（**关键是要看"上线后"的指标，不是单步指标**）

    · **最终长度分布**：短的一档 vs 长的一档，差得开不开（这就是"时短时长"）
    · **上线回声率**：最终回复里还有没有重复（目标 0）
    · **淘汰率**：多少补充因为回声被丢掉（太高说明补充没用）
    · **编造**：补充里还有没有「你上次也」（`strip_claims` 之后应为 0）

用法：
    SUP_ACTOR_API=... python tests/analysis/supplement_loop_probe.py
    （`LOOP_RUNS` 每题几遍；`LOOP_TH` 回声阈值，默认 0.3）
"""
from __future__ import annotations

import os
import pathlib
import re
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator          # noqa: E402
from engine.style import strip_claims          # noqa: E402

RUNS = max(1, int(os.environ.get("LOOP_RUNS", "2")))
TH = float(os.environ.get("LOOP_TH", "0.3"))

PROMPTS = [
    "今天累死了",
    "刚跟朋友吵了一架",
    "我在想要不要换工作",
    "我最近老是想哭",
    "周末不知道干嘛好",
    "我今天升职了",
    "我妈又催我相亲了",
    "我有点睡不着",
]

CLAIM_MARKS = ("你说过", "你提过", "你之前说", "你上次说",
               "上次也", "上回也", "以前也", "之前也",
               "上次说", "上次提", "你还说", "那次你", "你那次")

STOP = set("的了吗呢吧啊呀哦嗯是我你他她它们这那和就也都不没有在个一")
NOTHING = ("没有", "没了", "没什么", "不需要", "不必", "没啥", "够了", "就这样")


PUNCT = set("。，、；：！？!?…—～~「」『』（）()《》〈〉,.·-*#%&@/")

def grams(text: str) -> set:
    s = "".join(ch for ch in str(text or "") if ch not in PUNCT)
    return {s[i:i + 2] for i in range(len(s) - 1)
            if any(ch not in STOP for ch in s[i:i + 2])}


def overlap(a: str, b: str) -> float:
    gb = grams(b)
    return len(grams(a) & gb) / len(gb) if gb else 0.0


def main() -> int:
    api = os.environ.get("SUP_ACTOR_API") or os.environ.get("COMPANION_API_URL") \
        or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=api)
    print(f"两段式端到端　{len(PROMPTS)} 题 × {RUNS} 次　阈值 {TH}　{api}")
    print("=" * 92)

    short_lens, long_lens, all_lens = [], [], []
    dropped = echoed = fabricated = total = 0
    samples = []

    for q in PROMPTS:
        for _ in range(RUNS):
            total += 1
            gen.history = []
            try:
                first = str(gen.chat(q, remember=False) or "").strip()
            except Exception:
                first = ""
            ask = (f"{q}\n\n（我刚才已经回了这么一句：「{first}」）\n"
                   f"还有想补充的吗？只补充**这一轮能说得出的东西**："
                   f"不要提「你上次」「以前」这类我根本没说过的事，"
                   f"也不要重复我那句的意思，也不要念任何规则。"
                   f"有就补一句，没有就只回两个字：没有。")
            gen.history = []
            try:
                sup = str(gen.chat(ask, remember=False) or "").strip()
            except Exception:
                sup = "没有"

            final = first
            used = False
            if sup and not any(w in sup[:6] for w in NOTHING):
                # ④ 先剪编造：补充剪光就返回空（**不能**接一句"我记不清"兜底，
                #    否则会拼出「…你不是一个人扛的。这个我记不太清了」这种自相矛盾）
                sup2, fab = strip_claims(sup, CLAIM_MARKS, fallback="")
                if fab:
                    fabricated += 1
                # ③ 回声判定：**逐句比**，不能整段比。
                #    实测踩过：补充整体较长时，一小句原样复述会被全局重合度稀释到
                #    0.3 以下漏放（「想说什么都行」重复了两遍却判成"新"）。
                sents = [s for s in re.split(r"[。！？!?…]", sup2) if s.strip()]
                worst = max([overlap(first, s + "。") for s in sents], default=0.0)
                if (not sup2.strip()) or worst >= TH:
                    dropped += 1
                else:
                    final = first + sup2
                    used = True
            (long_lens if used else short_lens).append(len(final))
            all_lens.append(len(final))
            if len(samples) < 5:
                samples.append((q, first, sup, final, used))

    n_used = len(long_lens)
    print(f"  **采用补充 {n_used}/{total}（{100*n_used/max(1,total):.0f}%）**"
          f"　因回声丢掉 {dropped}　剪掉编造 {fabricated}")
    if short_lens:
        print(f"  短的那档（没接补充）　中位 **{statistics.median(short_lens):.0f}** 字"
              f"（{len(short_lens)} 轮）")
    if long_lens:
        print(f"  长的那档（接了补充）　中位 **{statistics.median(long_lens):.0f}** 字"
              f"（{len(long_lens)} 轮）")
    if short_lens and long_lens:
        gap = statistics.median(long_lens) - statistics.median(short_lens)
        print(f"  **两档差 {gap:.0f} 字**　← 这就是「时短时长」的幅度")
    print(f"  全体　中位 {statistics.median(all_lens):.0f}　区间 {min(all_lens)}~{max(all_lens)}")
    print()
    # 上线后的回声率（对最终文本自查）
    def worst_sent(a: str, b: str) -> float:
        ss = [x for x in re.split(r"[。！？!?…]", b) if x.strip()]
        return max([overlap(a, x + "。") for x in ss], default=0.0)
    shipped_echo = sum(1 for q, f, s, fin, u in samples
                       if u and worst_sent(f, s) >= TH)
    print(f"  **上线后仍带回声的：{shipped_echo}/{len(samples)}**（抽样 {len(samples)} 条，目标 0）")
    print()
    print("  === 样例 ===")
    for q, f, s, fin, used in samples:
        print(f"    {q}{'  【接补充】' if used else '  【没补】'}")
        print(f"      主：{f[:52]}")
        print(f"      补：{s[:52]}")
        print(f"      → {fin[:64]}")
        print()
    print("判读：")
    print("  · 采用率 30%~70% + 两档长度差 ≥10 字 → **双随机这条路成立**")
    print("  · 采用率过低 → 大部分补充是回声，等于没补")
    print("  · 上线回声 >0 → 阈值要调低（更严）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
