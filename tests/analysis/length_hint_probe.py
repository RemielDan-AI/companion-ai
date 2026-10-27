"""测「在提示词末尾注入字数限制」到底灵不灵（同一个模型、同一批问题，只换那最后一句）。

## 为什么先测再改

用户问："如果在提示词注入时添加字数限制呢，可以调整吗"。

机制是现成的——`Generator.system_prompt()` 有 `tail_prompt`，注释写着
「放在最末尾：小模型对**最后一句**的遵从率明显更高」。`say()` 也已经把它透传了。

但**"有参数"不等于"有效果"**。小模型忽略长度指令是很常见的失败。
所以先量：**同一批问题，只换末尾那句，回复长度会不会真的跟着变。**

## 四档对照

    ① 无提示（基线）
    ② 硬上限型：  "这次二十个字以内"
    ③ 角色型：    "这次就随口应一句，别展开"
    ④ 反向：      "这次多说两句，可以展开"

看两件事：
    · **中位长度跟不跟着指令走**（②应明显短于①，④应明显长于①）
    · **有没有截断**（硬上限容易造出半句——这是长度参数最常见的副作用）

用法：
    python tests/model_server.py run --model v1.5 --ngl 15 \
        --script tests/analysis/length_hint_probe.py
    （`LEN_RUNS` 控制每个条件问几遍）
"""
from __future__ import annotations

import os
import pathlib
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator  # noqa: E402

RUNS = max(1, int(os.environ.get("LEN_RUNS", "3")))

PROMPTS = [
    "今天累死了",
    "刚跟朋友吵了一架",
    "我在想要不要换工作",
    "周末不知道干嘛好",
    "我最近老是想哭",
    "外面下雨了",
]

CONDITIONS = [
    ("① 无提示", ""),
    ("② 硬上限20字", "这次二十个字以内。"),
    ("③ 角色型·别展开", "这次就随口应一句，别展开。"),
    ("④ 反向·多说两句", "这次多说两句，可以展开一点。"),
]


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"字数指令实测　{len(PROMPTS)} 题 × {RUNS} 次　模型 {os.environ.get('EXTRACT_MODEL_TAG', url)}")
    print("=" * 84)
    print(f"{'条件':<20}{'中位':>7}{'均值':>8}{'p90':>6}{'最长':>6}{'截断':>7}")
    print("-" * 84)
    rows = {}
    for name, tail in CONDITIONS:
        lengths, cuts = [], 0
        for q in PROMPTS:
            for _ in range(RUNS):
                try:
                    r = gen.chat(q, tail_prompt=tail, remember=False)
                except Exception as exc:
                    r = f"（出错 {exc}）"
                t = str(r or "").strip()
                lengths.append(len(t))
                # 截断：不以句末标点结尾，且长度接近上限
                if t and t[-1] not in "。！？!?…~」』”":
                    cuts += 1
        L = sorted(lengths)
        rows[name] = statistics.median(L)
        print(f"{name:<20}{statistics.median(L):>7.0f}{statistics.mean(L):>8.1f}"
              f"{L[int(len(L)*0.9)]:>6}{L[-1]:>6}{cuts:>7}")
    print()
    base = rows.get("① 无提示")
    if base:
        for name in ("② 硬上限20字", "③ 角色型·别展开", "④ 反向·多说两句"):
            if name in rows:
                d = rows[name] - base
                verdict = "✅ 跟着走" if (abs(d) >= 4) else "⚠ 几乎没反应"
                print(f"  {name:<18} 比基线 {d:+.0f} 字　{verdict}")
    print()
    print("判读：")
    print("  · ② 明显短、④ 明显长 → **这个杠杆好用**，可以按轮随机注入")
    print("  · 都没反应 → 提示词控不住长度，只能靠训练数据分布")
    print("  · ② 的『截断』数高 → 硬上限在造半句，改成角色型措辞（③）更安全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
