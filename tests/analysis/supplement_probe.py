"""测「先回一句，再问它有什么要补充」这个两段式值不值（用户的想法）。

## 想法

用户提的：**让它生成它说的那段话的补充**。

为什么这个思路比"在提示词里要求说长"高明：
  · 「这次多说两句」是**一条弱指令**——实测④几乎没效果（23 → 20 字），
    模型不是在产出内容，只是在被要求变长；
  · 「刚才那句话有什么想补充的」是**另一个任务**——它在回答一个新问题，
    有内容才补，没内容就说没有。**长度差异来自真实判断，不是随机数。**

## 这个探针量什么

    ① **补充率**：多少比例的轮次它真的补了（其余说"没有"）
    ② **补了多少字**（主回复 vs 补后总长）
    ③ **重复度**：补充和主回复的实义二元组重合率——**高了就是在车轱辘话**
    ④ **套话率**：补充里有没有「另外」「其实」「顺便说」这类填充词
    ⑤ **第二段耗时**（延迟成本）

## 判读

    · 补充率高 + 重合度低 + 套话少 → **值得接**，长度天然时短时长
    · 补充率低 → 它觉得没什么可补，那这条路等于没用
    · 重合度高 / 套话多 → 补充在灌水，比固定尾巴还糟

用法：
    python tests/model_server.py run --model v1.5 --ngl 15 \
        --script tests/analysis/supplement_probe.py
    （`SUP_RUNS` 控制每题几遍）
"""
from __future__ import annotations

import os
import pathlib
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
    "我妈又催我相亲了",
]

# 「没什么可补」的各种说法
NOTHING = ("没有", "没了", "没什么", "不用", "不需要", "就这样", "这话就够了",
           "不必", "没啥", "够了")
# 灌水腔
FILLER = ("另外", "此外", "顺便", "补充一点", "值得一提的是", "还有就是",
          "总的来说", "希望对你有帮助", "需要注意的是")

STOP = set("的了吗呢吧啊呀哦嗯是我你他她它们这那和就也都不没有在个一")

PUNCT = set("。，、；：！？!?…—～~「」『』（）()《》〈〉,.·-*#%&@/")

def grams(text: str) -> set:
    s = "".join(ch for ch in str(text or "") if ch not in PUNCT)
    return {s[i:i + 2] for i in range(len(s) - 1)
            if any(ch not in STOP for ch in s[i:i + 2])}


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"两段式补充实测　{len(PROMPTS)} 题 × {RUNS} 次　"
          f"模型 {os.environ.get('EXTRACT_MODEL_TAG', url)}")
    print("=" * 92)

    main_lens, sup_lens, total_lens = [], [], []
    supplemented = 0
    overlaps, fillers = [], 0
    sec2_times = []
    samples = []

    for q in PROMPTS:
        for _ in range(RUNS):
            try:
                first = str(gen.chat(q, remember=False) or "").strip()
            except Exception as exc:
                first = f"（出错 {exc}）"
            main_lens.append(len(first))
            # 第二段：把「刚才说的」连同用户的话一起给它
            # ⚠ 第二版提示词：堵住第一版实测到的两个毛病
            #   ① **编造历史**（"你上次也这么说的"）→ 明确点名禁止
            #   ② **提示词泄漏**（把风格规则当内容说出来）→ 明确禁止念规则
            ask = (f"{q}\n\n（我刚才已经回了这么一句：「{first}」）\n"
                   f"还有想补充的吗？只补充**这一轮能说得出的东西**："
                   f"不要提「你上次」「以前」这类我根本没说过的事，"
                   f"也不要重复我那句的意思，也不要念任何规则。"
                   f"有就补一句，没有就只回两个字：没有。")
            t0 = time.time()
            try:
                second = str(gen.chat(ask, remember=False) or "").strip()
            except Exception as exc:
                second = "没有"
            sec2_times.append(time.time() - t0)
            sup_lens.append(len(second))

            if second and not any(w in second[:6] for w in NOTHING):
                supplemented += 1
                total_lens.append(len(first) + len(second))
                # 重复度：补充里有多少二元组已经在主回复里
                a, b = grams(first), grams(second)
                if b:
                    overlaps.append(len(a & b) / len(b))
                if any(w in second for w in FILLER):
                    fillers += 1
                if len(samples) < 4:
                    samples.append((q, first, second))
            else:
                total_lens.append(len(first))

    n = len(main_lens)
    print(f"  主回复　中位 **{statistics.median(main_lens):.0f}** 字　均值 {statistics.mean(main_lens):.1f}")
    print(f"  **补充率　{supplemented}/{n}（{100*supplemented/n:.0f}%）**")
    if supplemented:
        print(f"  补充段长度　中位 {statistics.median(sup_lens):.0f} 字")
        print(f"  **补充后总长　中位 {statistics.median(total_lens):.0f} 字**"
              f"（未补的保持 {statistics.median(main_lens):.0f}）")
        print(f"  **与主回复的二元组重合　中位 {100*statistics.median(overlaps):.0f}%**"
              f"（越高越是车轱辘话）")
        print(f"  含灌水套话的补充　{fillers}/{supplemented}")
    else:
        print("  它几乎总是说「没有」——这条路等于没用")
    print(f"  第二段耗时　中位 {statistics.median(sec2_times):.1f}s"
          f"（延迟成本；第一段之外多花的）")
    print()
    if samples:
        print("  === 真的补了的样例 ===")
        for q, a, b in samples:
            print(f"    问：{q}")
            print(f"      主：{a[:60]}")
            print(f"      补：{b[:70]}")
            print()
    print("判读：")
    print("  · 补充率 ≥30% + 重合 <40% + 套话少 → **值得接**")
    print("  · 补充率高但重合高 → 在车轱辘话，比固定尾巴更糟")
    print("  · 补充率低 → 它觉得没得补，这条思路本身不成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
