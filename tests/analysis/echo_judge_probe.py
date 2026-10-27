"""测「4B 监督官能不能判出回声」——用户设想：能判就能把双随机（补充次数 × 长度档）救回来。

## 为什么这次可能成

前面实测过：用同一个 4B 当**导演**（现场生成一句风格指令）——**失败**：
6 条指令一字不差全是「软一点，先接情绪」，而且带指令的组（-5 字）
反而不如**随机固定指令**那组（-8 字）。**它没有判断力。**

但"**判有没有回声**"是**分类题**，不是开放生成题：
    · 输出空间只有两个（重复 / 新）
    · **有确定性对照答案**（实义二元组重合度），可以算准确率
    · 判错代价不对称：**判成"重复"→ 丢掉补充（退化成短回复，安全）**；
      判成"新"→ 回声上线（难看）。所以合适的阈值要把**精确率**放前面。

## 测法

从真实样例里攒一批 `(前面说过的话, 新的补充)` 对，人工标 重复/新，
再让 4B 判，和**二元组重合度**这个确定性指标对照：

    · 4B 的准确率 / 精确率 / 召回
    · **二元组阈值**能不能单独胜任（能的话根本不用模型）

判读：
    · 4B 明显强于阈值 → 值得用（贵一点但准）
    · 差不多 → **用阈值**（免费、确定性、可回滚）
"""
from __future__ import annotations

import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator  # noqa: E402

# (前面说过的话, 新补充, 人工标签 True=重复)
PAIRS = [
    # ---- 明显重复（实测里就是这么抄的）----
    ("你上次也吵了。这次是因为什么？", "你上次也吵了。这次是因为什么？", True),
    ("嗯，我在听。你不用说话，躺着也行。", "嗯，我在听。你想说什么直接说，不想说也没关系。", True),
    ("好。那就不当回事。", "好，那就不当回事。", True),
    ("你上次也闹了。这次是为谁？", "你上次也闹了。这次是为谁？", True),
    ("嗯，知道。这种时候最难的是不让自己也跟着一起动。",
     "你上次也说这种时候最难的是不让自己动。", True),
    # ---- 换了说法但意思一样（**难例**：二元组可能不高，人看还是重复）----
    ("先歇着吧，别急着动。", "到家先歇着，别急着动。", True),
    ("你今天够呛。", "你今天够呛的。", True),
    # ---- 真正的新内容 ----
    ("嗯，听出来了。想说什么就说。", "要不先吃点东西？饿着更难受。", False),
    ("吵了就吵了，你现在愿意跟我说。", "你们是为钱还是为别的事？", False),
    ("换工作这事急不得。", "你手上现在的存款够撑几个月？", False),
    ("躺着也行，我陪着你。", "要不要放点白噪音？我这儿有雨声。", False),
    ("嗯，我在听。", "明天要是还这么累，就跟我说一声。", False),
    # ---- 边界：说的是同类话题但不是复述 ----
    ("今天累死了。", "地铁挤是主因吧？", False),
    ("你心里还难受吗？", "难受就哭出来，别憋着。", False),
]

BOSS_SYSTEM = (
    "你是检查员。给你两段：第一段是**刚才说过的**，第二段是**准备补充的**。\n"
    "只需要判断第二段是不是**在重复第一段的意思**（换了说法但意思一样也算重复）。\n"
    "只回一个字：重复 / 新。不要解释。"
)

STOP = set("的了吗呢吧啊呀哦嗯是我你他她它们这那和就也都不没有在个一")


PUNCT = set("。，、；：！？!?…—～~「」『』（）()《》〈〉,.·-*#%&@/")

def grams(text: str) -> set:
    s = "".join(ch for ch in str(text or "") if ch not in PUNCT)
    return {s[i:i + 2] for i in range(len(s) - 1)
            if any(ch not in STOP for ch in s[i:i + 2])}


def overlap(a: str, b: str) -> float:
    gb = grams(b)
    return len(grams(a) & gb) / len(gb) if gb else 0.0


def main() -> int:
    api = os.environ.get("SUP_BOSS_API") or "http://127.0.0.1:8084/v1/chat/completions"
    gen = Generator(api_url=api)
    print(f"回声判定　{len(PAIRS)} 对　监督官 {api}")
    print("=" * 92)
    print(f"{'重叠':>6}{'人工':>6}{'4B判':>8}{'':>4}  前面 / 补充")
    print("-" * 92)

    tp = fp = tn = fn = 0
    rows = []
    for a, b, is_dup in PAIRS:
        ov = overlap(a, b)
        ask = f"第一段（刚才说过的）：{a}\n第二段（准备补充的）：{b}"
        try:
            raw = str(gen.complete(BOSS_SYSTEM, ask, max_tokens=8, temperature=0.0) or "")
        except Exception as exc:
            raw = f"（出错 {exc}）"
        said_dup = "重复" in raw
        if is_dup and said_dup:
            tp += 1
        elif not is_dup and said_dup:
            fp += 1
        elif not is_dup and not said_dup:
            tn += 1
        else:
            fn += 1
        rows.append((ov, is_dup, said_dup, a, b))
        mark = "✅" if said_dup == is_dup else "✗"
        print(f"{100*ov:>5.0f}%{('重复' if is_dup else '新'):>6}"
              f"{('重复' if said_dup else '新'):>8}{mark:>4}  {a[:26]} / {b[:26]}")

    n = len(PAIRS)
    acc = (tp + tn) / n
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0
    print()
    print(f"  **4B 准确率 {acc*100:.0f}%　精确率 {prec*100:.0f}%　召回 {rec*100:.0f}%**"
          f"（TP{tp} FP{fp} TN{tn} FN{fn}）")

    # 对照：纯二元组阈值能到多少
    best = None
    for th in (0.3, 0.4, 0.5, 0.6, 0.7):
        t2 = f2 = tn2 = fn2 = 0
        for ov, is_dup, _s, _a, _b in rows:
            pred = ov >= th
            if is_dup and pred:
                t2 += 1
            elif not is_dup and pred:
                f2 += 1
            elif not is_dup and not pred:
                tn2 += 1
            else:
                fn2 += 1
        a2 = (t2 + tn2) / n
        p2 = t2 / (t2 + f2) if (t2 + f2) else 1.0
        r2 = t2 / (t2 + fn2) if (t2 + fn2) else 1.0
        print(f"    阈值 {th:.1f}：准确 {a2*100:.0f}%  精确 {p2*100:.0f}%  召回 {r2*100:.0f}%")
        if best is None or a2 > best[1]:
            best = (th, a2, p2, r2)
    print(f"  → 二元组最好：阈值 {best[0]}，准确 **{best[1]*100:.0f}%**"
          f"（精确 {best[2]*100:.0f}% 召回 {best[3]*100:.0f}%）")
    print()
    print("判读：")
    print("  · 4B 准确率明显高于阈值 → 用模型判（贵但准）")
    print("  · 差不多 → **用阈值**：免费、确定性、可回滚，不用请监督官")
    print("  · 4B 的**精确率**更要紧：判成『重复』只是丢掉补充（安全），")
    print("    判成『新』才会让回声上线（难看）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
