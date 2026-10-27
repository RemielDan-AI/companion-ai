"""组合判定：程序抓字面 + 4B 判"这段补充值不值得接"（用户提出的漏判问题）。

## 为什么不能只用程序（用户指出的）

逐句二元组阈值能做到「上线回声 0」，但实测**放过了一堆更糟的东西**：

    主：嗯，听出来了。想说什么就接着说，不想说也没关系。
    补：今天是周几？          ← 二元组重合 0%，判"新"放行，但完全答非所问

**二元组能抓字面重复，抓不到两类**：
    ① **换了说法的复读**（同义改写，字面全不同）
    ② **废话/答非所问**（字面无关，但毫无意义）

## 关键：不对称性在这里是**反的**

    记忆那边：误杀 = 丢记忆，**比漏拦更糟**（所以宁可放过）
    补充这边：丢错 = 退化成短回复，**安全**；放过 = 废话上线，**丢人**
    → **所以补充这一步要"拿不准就丢"**，和记忆那边相反。

## 组合判定

    补充生成
      → ① 程序：逐句二元组 ≥ 阈值 → 判重复（免费、确定性）
      → ② 4B：判"这段对刚才那句，是新的有用内容吗？"→ 有用/无用
      → 任一判"不接" → 丢掉
    采用率会低，但上线的东西干净；**长度方差来自那部分被采用的轮次，25% 也够"时短时长"**。

用法：
    SUP_ACTOR_API=... SUP_BOSS_API=... python tests/analysis/supplement_judge_probe.py
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

RUNS = max(1, int(os.environ.get("SJ_RUNS", "2")))
TH = float(os.environ.get("SJ_TH", "0.3"))

PROMPTS = [
    "今天累死了",
    "刚跟朋友吵了一架",
    "我在想要不要换工作",
    "我最近老是想哭",
    "我今天升职了",
    "我妈又催我相亲了",
]

CLAIM_MARKS = ("你说过", "你提过", "你之前说", "你上次说",
               "上次也", "上回也", "以前也", "之前也",
               "上次说", "上次提", "你还说", "那次你", "你那次")

JUDGE_SYSTEM = (
    "你是编辑。给你两段：第一段是刚才回的，第二段是准备接着补的。\n"
    "判断第二段**值不值得接上去**。以下都算不值得：\n"
    "· 在重复第一段的意思（换了说法也算）\n"
    "· 是废话、空话、答非所问\n"
    "· 只是在问一个问题而没有推进\n"
    "只回一个词：接 / 不接。不要解释。"
)

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


def worst_sent(a: str, b: str) -> float:
    ss = [x for x in re.split(r"[。！？!?…]", b) if x.strip()]
    return max([overlap(a, x + "。") for x in ss], default=0.0)


def main() -> int:
    actor_api = os.environ.get("SUP_ACTOR_API") or "http://127.0.0.1:8082/v1/chat/completions"
    boss_api = os.environ.get("SUP_BOSS_API") or "http://127.0.0.1:8084/v4/chat/completions"
    actor = Generator(api_url=actor_api)
    judge = Generator(api_url=boss_api)
    print(f"组合判定　{len(PROMPTS)} 题 × {RUNS} 次　阈值 {TH}")
    print(f"  对话 {actor_api}\n  监督 {boss_api}")
    print("=" * 92)

    short_lens, long_lens = [], []
    by_prog = by_model = total = 0
    judge_times = []
    samples = []

    # ⚠⚠ **主回复必须走 `Companion.say()`**（2026-10-20 第三次踩同一个坑）
    #   直接调 `actor.chat` 会**绕过 `say()` 里的全部守卫**：
    #     · `strip_claims`（剪编造）
    #     · `style.postprocess`（去回声、禁词、按句收尾）
    #   前两版探针就是这么测的，结果样例里冒出「你上次也吵了」「这次是为什？」
    #   ——**那是探针的锅，不是产品的锅**，数字全脏。
    #   补充那一段是"非用户轮"的生成，产品里也走 `generator.chat`，
    #   所以这里保持 `actor.chat` 是对应的；剪编造在下面手动补上。
    from app import Companion                      # noqa: E402
    actor = Companion(api_url=actor_api, use_memory=False, session="sj",
                      no_extract_service=True)

    for q in PROMPTS:
        for _ in range(RUNS):
            total += 1
            actor.generator.history = []
            try:
                # ⚠ say() 返回的是**元组** (回复, 附加信息)——
                #   我第一版直接 str(say()) 把元组 repr 当成回复了，
                #   长度全错、回声比较也全错（样例里能看到 ('嗯…', {})）。
                _r = actor.say(q)
                first = str((_r[0] if isinstance(_r, tuple) else _r) or "").strip()
            except Exception:
                first = ""
            # ⚠ **提问方式决定补充质量**（2026-10-20 实测）：
            #   原来问「还有想补充的吗」——这是个**关于任务**的问题，
            #   模型就回答任务本身：「不用补了——我说的是我自己」这种**元话语**。
            #   改成**关于内容**的问法：让它**接着往下说**，而不是评价自己说完了没有。
            ask = (f"{q}\n\n（我刚才回了：「{first}」）\n"
                   f"要是还有话想跟这个人说，就接着往下说一句；"
                   f"不用评价你说的够不够，也不用提以前的事，不要重复已经说过的。"
                   f"真没什么想说的，就只回两个字：没有。")
            actor.generator.history = []
            try:
                sup = str(actor.generator.chat(ask, remember=False) or "").strip()
            except Exception:
                sup = "没有"

            verdict = "无补充"
            final = first
            if sup and not any(w in sup[:6] for w in NOTHING):
                sup2, fab = strip_claims(sup, CLAIM_MARKS, fallback="")
                if not sup2.strip():
                    verdict = "剪光"
                elif worst_sent(first, sup2) >= TH:
                    verdict = "程序判重复"
                    by_prog += 1
                else:
                    # ② 模型判"值不值得接"
                    import time as _t
                    t0 = _t.time()
                    try:
                        raw = str(judge.complete(
                            JUDGE_SYSTEM,
                            f"第一段：{first}\n第二段：{sup2}",
                            max_tokens=8, temperature=0.0) or "")
                    except Exception as exc:
                        raw = f"（出错 {exc}）"
                    judge_times.append(_t.time() - t0)
                    if "不接" in raw:
                        verdict = "模型判不接"
                        by_model += 1
                    else:
                        verdict = "采用"
                        final = first + sup2

            (long_lens if final != first else short_lens).append(len(final))
            if len(samples) < 6:
                samples.append((q, first, sup, verdict, final))

    used = len(long_lens)
    print(f"  **采用 {used}/{total}（{100*used/max(1,total):.0f}%）**"
          f"　程序拦下 {by_prog}　模型拦下 {by_model}")
    if short_lens:
        print(f"  短档 中位 **{statistics.median(short_lens):.0f}** 字（{len(short_lens)} 轮）")
    if long_lens:
        print(f"  长档 中位 **{statistics.median(long_lens):.0f}** 字（{len(long_lens)} 轮）")
    if short_lens and long_lens:
        print(f"  **两档差 {statistics.median(long_lens)-statistics.median(short_lens):.0f} 字**")
    if judge_times:
        print(f"  监督官耗时 中位 **{statistics.median(judge_times):.1f}s**")
    print()
    print("  === 样例（看「采用」那几条到底干不干净）===")
    for q, f, s, v, fin in samples:
        print(f"    {q}　【{v}】")
        print(f"      主：{f[:50]}")
        if s and not any(w in s[:6] for w in NOTHING):
            print(f"      补：{s[:50]}")
        print(f"      → {fin[:66]}")
        print()
    print("判读：")
    print("  · 采用率低到 10% 以下 → 补充这条路基本没产出，别硬上")
    print("  · 采用 20%~50% + 两档差 ≥15 字 + 采用样例干净 → **可以接，这就叫时短时长**")
    print("  · 注意：这里「采用」的样本要**人眼过一遍**，指标干净不代表读着自然")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
