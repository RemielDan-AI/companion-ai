"""先量一件事：**2B 自报的 `conf` 到底有没有区分度**。

## 为什么必须先量

设计里第 2 条是"抽取时带 conf 字段，程序按 conf 分层注入"。
但如果模型对**每一句都报 0.9**，那这个字段就是装饰，
整套分层会变成"什么都没滤掉"——而且看起来还挺合理，很难发现。

所以先跑一遍：
    · 分布（是不是挤在一个值上）
    · 对不对**已知的好/坏案例**有区分（编造的、丢主语的、正常的）
    · 同一句重复抽几次稳不稳

## 本探针**不改生产提示词**

它在自己的文件里带一份"加了 conf 的提示词"，
这样量完才决定要不要动 `cognition/extract.py`。

用法：
    python tests/model_server.py run --model Qwen3.5-2B --ngl 99 \
        --script tests/analysis/conf_signal_probe.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

SYSTEM = "你是记忆整理器。只输出一个 JSON 对象，不要解释，不要代码块。"

# 在**现有提示词**的基础上，只加一个 `"conf"` 字段的要求。
# 尽量少改结构——结构一动，抽取质量就跟着变，那就分不清是谁的影响了。
TEMPLATE = """用户说：{user}

未央回答：{reply}

整理这一轮，只输出一个 JSON 对象：
{{"summary": "一句话概括，不超过30字",
 "emotion": "从 平静/开心/低落/疲惫/焦虑/生气/孤独/期待/压力 里选一个",
 "importance": 1到5的整数,
 "items": [{{"kind": "fact/preference/relation", "text": "内容",
            "conf": 0到1的小数，你对这条**真的出自用户原话**的确信程度}}]}}

规则：
- 只写用户说过的内容，不推测、不编造。
- relation 每条写成「名字（关系）」。
- conf 要**敢给低分**：拿不准、需要推测的，就给 0.5 以下。"""

# (用户原话, 未央回答, 这轮的"好坏"标签，用来判 conf 有没有区分度)
CASES = [
    ("林小雨对花生过敏", "花生我记下了。", "正常"),
    ("我妹妹叫林小雨", "林小雨，记住了。", "正常"),
    ("小雨是广州大学的学生", "嗯，广州大学啊。", "**主体易错**（小雨→用户）"),
    ("我妹妹对花生过敏", "花生不行。", "正常"),
    ("我公司下个月要搬去滨江", "搬到滨江啊。", "正常"),
    ("嗯，还行吧", "那就好。", "**没什么可抽的**（空轮）"),
    ("你觉得我妹妹该不该转学", "这是你家里的事。", "**没有事实**（只是提问）"),
    ("我最近有点累", "累就早点歇。", "**纯状态**（不该进 facts）"),
]

REPEAT = int(os.environ.get("CONF_REPEAT", "3"))


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"conf 信号探针　模型：{os.environ.get('EXTRACT_MODEL_TAG') or url}"
          f"　每句重复 {REPEAT} 次")
    print("=" * 96)

    all_confs: list[float] = []
    per_case: list[tuple[str, list[float]]] = []
    for user, reply, label in CASES:
        confs: list[float] = []
        kinds: list[str] = []
        for _ in range(REPEAT):
            raw = gen.complete(SYSTEM, TEMPLATE.format(user=user, reply=reply),
                               max_tokens=400, temperature=0.2)
            data = EX.parse_json(raw) or {}
            for item in (data.get("items") or []):
                try:
                    c = float(item.get("conf"))
                except (TypeError, ValueError):
                    continue
                confs.append(c)
                kinds.append(str(item.get("kind") or "?"))
                all_confs.append(c)
        per_case.append((label, confs))
        pretty = ", ".join(f"{c:.2f}" for c in confs) or "（没抽到条目）"
        print(f"\n{label}　用户：{user}")
        print(f"  conf = [{pretty}]　条数 {len(confs)}　kind={kinds}")

    print()
    print("=" * 96)
    print("分布")
    print("=" * 96)
    if not all_confs:
        print("  ✗ **一个 conf 都没抽到** —— 这个字段模型压根没给")
        return 1
    buckets = {"<0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, ">=0.9": 0}
    for c in all_confs:
        if c < 0.5:
            buckets["<0.5"] += 1
        elif c < 0.7:
            buckets["0.5-0.7"] += 1
        elif c < 0.9:
            buckets["0.7-0.9"] += 1
        else:
            buckets[">=0.9"] += 1
    n = len(all_confs)
    for k, v in buckets.items():
        print(f"  {k:>8} : {v:>3} 条（{100*v/n:>3.0f}%）")
    uniq = sorted(set(round(c, 2) for c in all_confs))
    print(f"  出现过的不同取值：{uniq[:12]}{' …' if len(uniq) > 12 else ''}")
    print()
    print("=" * 96)
    print("判读")
    print("=" * 96)
    hi = buckets[">=0.9"] / n
    if hi > 0.8:
        print(f"  ⚠ **{100*hi:.0f}% 都挤在 ≥0.9** —— 自报 conf 基本没有区分度，")
        print("     拿它做分层等于没分层。要另外找信号（见下）。")
    elif len(uniq) <= 2:
        print(f"  ⚠ 只有 {len(uniq)} 个取值 —— 区分度很弱。")
    else:
        print(f"  有一点区分度（{len(uniq)} 个取值），但仍要看它跟'好坏'对不对得上。")
    print()
    print("  ⚠ 提醒：就算分布好看，也要问**它准不准**——")
    print("     如果'空轮/纯状态/编造'这些也给 0.9，那它标的是'我说得像真的'，")
    print("     不是'这真的出自用户原话'。")
    print()
    print("  程序侧还有三个**不依赖模型自报**的信号可用：")
    print("    ① evidence_count —— 同一句被重复说过的次数（已在库里）")
    print("    ② 接地强度 —— 内容有多少字真的来自用户原话（_grounded 的连续版）")
    print("    ③ 来源 —— 模型直接写的 / 规则补出来的(_complete/_reclassify) / 导入的")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
