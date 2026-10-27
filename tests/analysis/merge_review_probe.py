"""测「把多轮回答合并再问有哪些要补充」——用户的第二个想法。

## 和「补充刚才那句」的区别（这是关键）

第一版（`supplement_probe.py`）问的是"我刚才那句话说完了，还有想补充的吗"，
模型把它当**续写**任务 → 接着那句往下说 → **复述开头**（v2 实测重合 **36%**）。

用户这次的想法是：**把几轮回答合并起来**，再问"这些话有哪些需要补充"。
任务性质从**续写**变成**查漏**——它要找的是"整体上还缺什么"，
而不是"接着刚才那句说什么"。上下文里**没有单独一句话可抄**，回声就没了来源。

## ⚠ 但有个新风险必须量

审查式提问很容易招来**列表腔**（"首先…其次…1. 2. 3."），那是助手腔。
所以除了原来的指标，这里**专门数列表标记**。

## 量什么

    ① 补充率（真的提出东西的比例）
    ② **与合并文本的二元组重合**（回声）
    ③ **列表/条目化**（`1.` `2.` `首先` `其次` `另外` `•` `-`）
    ④ 灌水套话
    ⑤ 补充长度、第二段耗时

用法：
    python tests/model_server.py run --model v1.5 --ngl 15 \
        --script tests/analysis/merge_review_probe.py
    （`MR_TURNS` = 合并几轮，默认 3；`MR_RUNS` 控制重复）
"""
from __future__ import annotations

import os
import pathlib
import re
import statistics
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator  # noqa: E402

RUNS = max(1, int(os.environ.get("MR_RUNS", "2")))
TURNS = max(2, int(os.environ.get("MR_TURNS", "3")))

# 每组是一段连续对话（3 轮），模拟真实陪伴场景
CONVERSATIONS = [
    ["今天累死了", "地铁上还挤得要命", "到家就想瘫着"],
    ["刚跟朋友吵了一架", "其实也不是大事", "就是觉得委屈"],
    ["我在想要不要换工作", "现在这个太累了", "但又怕换了下家更差"],
]

NOTHING = ("没有", "没了", "没什么", "不需要", "不必", "没啥", "够了", "就这样")
FILLER = ("另外", "此外", "顺便", "补充一点", "值得一提的是", "总的来说",
          "希望对你有帮助", "需要注意的是", "补充说明")
# 列表/条目化 = 助手腔
LIST_MARKS = (re.compile(r"^\s*\d+[.、)]"), re.compile(r"^\s*[-•*]\s"),
              re.compile(r"^\s*\(" + r"\d+" + r"\)"))
LIST_WORDS = ("首先", "其次", "再次", "第三", "最后一点", "第一，", "第二，")

STOP = set("的了吗呢吧啊呀哦嗯是我你他她它们这那和就也都不没有在个一")


PUNCT = set("。，、；：！？!?…—～~「」『』（）()《》〈〉,.·-*#%&@/")

def grams(text: str) -> set:
    s = "".join(ch for ch in str(text or "") if ch not in PUNCT)
    return {s[i:i + 2] for i in range(len(s) - 1)
            if any(ch not in STOP for ch in s[i:i + 2])}


def looks_like_list(text: str) -> bool:
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) >= 2 and sum(1 for ln in lines if any(p.match(ln) for p in LIST_MARKS)) >= 2:
        return True
    return any(w in str(text or "") for w in LIST_WORDS)


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"合并查漏实测　{len(CONVERSATIONS)} 组 × {RUNS} 次　合并 {TURNS} 轮　"
          f"模型 {os.environ.get('EXTRACT_MODEL_TAG', url)}")
    print("=" * 92)

    sup_count = total = 0
    overlaps, lengths, times = [], [], []
    listy = filler_hit = 0
    samples = []

    for conv in CONVERSATIONS:
        turns = conv[:TURNS]
        for _ in range(RUNS):
            said: list[tuple[str, str]] = []
            for u in turns:
                try:
                    a = str(gen.chat(u, remember=False) or "").strip()
                except Exception:
                    a = ""
                said.append((u, a))
            merged_user = "\n".join(u for u, _ in said)
            merged_ai = "\n".join(a for _, a in said)
            ask = (f"（前面几轮我说：）\n{merged_user}\n\n"
                   f"（你分别回了：）\n{merged_ai}\n\n"
                   f"把上面你说的这些合起来看，**还有哪些没说到、该补一句的**？"
                   f"只挑最关键的一处，一句话说完；没有就只回两个字：没有。"
                   f"不要重复上面已经说过的内容，不要列点、不要分条。")
            t0 = time.time()
            try:
                out = str(gen.chat(ask, remember=False) or "").strip()
            except Exception:
                out = "没有"
            times.append(time.time() - t0)
            total += 1
            if out and not any(w in out[:6] for w in NOTHING):
                sup_count += 1
                lengths.append(len(out))
                a, b = grams(merged_ai), grams(out)
                if b:
                    overlaps.append(len(a & b) / len(b))
                if looks_like_list(out):
                    listy += 1
                if any(w in out for w in FILLER):
                    filler_hit += 1
                if len(samples) < 4:
                    samples.append((merged_user.replace("\n", " / "), merged_ai.replace("\n", " / "), out))

    print(f"  **提出补充　{sup_count}/{total}（{100*sup_count/max(1,total):.0f}%）**")
    if lengths:
        print(f"  补充长度　中位 {statistics.median(lengths):.0f} 字")
        print(f"  **与合并文本重合　中位 {100*statistics.median(overlaps):.0f}%**"
              f"（对照：单句补充 v2 是 36%）")
        print(f"  **列表/条目化　{listy}/{sup_count}**（助手腔风险）")
        print(f"  灌水套话　{filler_hit}/{sup_count}")
    print(f"  耗时　中位 {statistics.median(times):.1f}s")
    print()
    if samples:
        print("  === 样例 ===")
        for u, a, o in samples:
            print(f"    用户：{u[:56]}")
            print(f"    说过：{a[:64]}")
            print(f"    补充：{o[:72]}")
            print()
    print("判读：")
    print("  · 重合明显低于 36% + 列表少 → **这个提问方式确实压住了回声**")
    print("  · 列表多 → 查漏式提问把助手腔招回来了，得加一句「别分条」")
    print("  · 补充率低 → 它觉得没得补")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
