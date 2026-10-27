"""校验 `training/export/memory_sft.jsonl` 这批**记忆 SFT 样本**还是好的。

## 格式：Alpaca 三字段（2026-10-19 纠正过）

⚠ **真正在用的训练集**是 `C:\\project\\微调数据\\主要数据\\weiyang_train.jsonl`
（**1085 条**，字段 `instruction` / `input` / `output`），
不是 `companion-ai/training/export/mined_sft.jsonl`（那是另一个 405 条的导出物）。

我第一版把样本生成成 `messages` 格式，**和那个训练集合不进去**，返工成 Alpaca。
这批的 `instruction` = **现有那句扮演指令（逐字）+ 记忆块**，所以能直接追加。

## 为什么要这个测试

训练数据错了比没数据更糟——它会把错误**固化进模型**。实测真的踩过一次：

    问「我妹妹对什么过敏」，答案被我写成「**你**对花生过敏」   ← 主语串了

进训练集就是在教它**把别人的事说成用户的**。所以要有这道闸。

## 查什么

    ① 结构：`instruction` / `input` / `output` 三个字段都在，且 `instruction` 以
       现有那句扮演指令**开头**（不同构就合不进训练集）
    ② **instruction 里真的有记忆块**（正向样本）——这正是这批数据的全部意义：
       实测现有 1085 条里 **0 条含记忆相关字样**，模型从没学过"用块"
    ③ `output` 里**不许**出现念提示词的措辞（"根据我的记忆"之类）
    ④ **主语校验**：问第三方的事，答案要么点到那个人，要么是纯裸答案；
       **不许**只写「你…」（那就是串人）
    ⑤ `block_must` 在 instruction 里、`block_absent` 不在（重新核对一遍）

不连模型、不起服务、很快。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DATA = ROOT / "training" / "export" / "memory_sft.jsonl"
BLOCK_MARK = "以下是关于对方的记忆"
BAD_PHRASES = ("根据我的记忆", "根据记忆", "我的记忆中", "系统提示", "记忆块",
               "检索到", "上下文里", "以上是我")
# 必须和 `weiyang_train.jsonl` 里那句一致（1085 条都是它）
INSTRUCTION = ("你现在扮演未央，一个温柔但有原则的陪伴者。"
               "用自然、有温度的中文回应，不要助手腔，不要说教。")
SUBJECT_HINT = ("妹妹", "林小雨", "同事", "陈思远", "妈", "猫", "团子")

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 90)
    print(f"记忆 SFT 样本校验　{DATA.name}")
    print("=" * 90)
    if not DATA.exists():
        # ⚠ 2026-10-27：这批样本是**从真实对话里挖出来的**，属于部署产物，
        #   不进公开仓库（含真实使用痕迹）。所以**没有就跳过，不算失败**——
        #   不然别人 clone 下来第一件事就是一片红。
        #   想跑这条：把数据放到该路径即可（生成脚本见 training/build_memory_sft.py）。
        print(f"⏭ 跳过：没有 {DATA.name}（训练样本是部署产物，不进仓库）")
        print("   要跑这条校验，先把它放到 " + str(DATA.parent))
        return 0
    rows = [json.loads(x) for x in DATA.read_text(encoding="utf-8").splitlines() if x.strip()]
    print(f"  共 {len(rows)} 条")
    check(len(rows) >= 30, "样本数够用（≥30）", f"{len(rows)}")

    bad_struct = [r for r in rows
                  if not all(k in r for k in ("instruction", "input", "output"))]
    check(not bad_struct, "结构都是 instruction / input / output 三字段",
          f"{len(bad_struct)} 条不符")

    # ⚠ 必须和现有训练集**同构**：instruction 以那句扮演指令开头 + 接块
    off_head = [r for r in rows if not r["instruction"].startswith(INSTRUCTION)]
    check(not off_head, "**instruction 以现有那句扮演指令开头**（同构才能合进训练集）",
          f"{len(off_head)} 条不匹配")

    with_block = [r for r in rows if BLOCK_MARK in r["instruction"]]
    no_block_ok = [r for r in rows if BLOCK_MARK not in r["instruction"]
                   and r.get("allow_empty_block")]
    check(len(with_block) + len(no_block_ok) == len(rows),
          "**每条要么带记忆块、要么是「允许空块」的负样本**",
          f"带块 {len(with_block)}　空块负样本 {len(no_block_ok)}")

    said = [r for r in rows if any(p in r["output"] for p in BAD_PHRASES)]
    check(not said, "答案里没有念提示词的措辞", f"{len(said)} 条")

    # 主语：问第三方，答案不许只写「你…」
    def third_party(r: dict) -> bool:
        return any(w in r["input"] for w in
                   ("妹妹", "林小雨", "同事", "陈思远", "妈", "猫", "团子"))

    swapped = []
    for r in rows:
        if r.get("category") in ("块里没有", "不主动倒", "状态变更"):
            continue
        if not third_party(r):
            continue
        ans = r["output"]
        if "你" in ans and not any(w in ans for w in SUBJECT_HINT):
            swapped.append(f"{r['input']} → {ans}")
    check(not swapped, "**没有「把别人的事说成用户的」**", str(swapped[:2]))

    miss = []
    no_fields = []
    for r in rows:
        # ⚠ **校验字段必须存在**：不给的话下面两个循环会"空过"，
        #   看起来全绿其实什么都没查——实测踩过（加了字段忘了重新生成，测试假绿）。
        if "block_must" not in r or "block_absent" not in r:
            no_fields.append(r["input"])
            continue
        for w in (r.get("block_must") or []):
            if w and w not in r["instruction"]:
                miss.append(f"{r['input']}：缺 {w}")
        for w in (r.get("block_absent") or []):
            if w and w in r["instruction"]:
                miss.append(f"{r['input']}：不该有 {w}")
    check(not no_fields, "**每条都带校验字段**（否则下面一项是空过）",
          str(no_fields[:2]))
    check(not miss, "block_must / block_absent 仍对得上", str(miss[:2]))

    cats = {}
    for r in rows:
        cats[r.get("category", "?")] = cats.get(r.get("category", "?"), 0) + 1
    want = {"直接事实", "计数聚合", "多主体", "否定例外",
            "状态变更", "多跳", "块里没有", "不主动倒"}
    check(want <= set(cats), "八类都覆盖到了", "")
    print()
    for k, v in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"      {k:<10}{v:>3} 条")

    print()
    print("=" * 90)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    print()
    print("⚠ 现有 1085 条训练样本里 **0 条含记忆相关字样**（instruction 全是同一句），")
    print("  所以模型从没学过「读块取答案」。这批是同构的 Alpaca 三字段，可直接追加。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
