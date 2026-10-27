#!/usr/bin/env python
"""审计判定变更的**分歧轮**：旧判据说拒绝、新判据说没有（或反之）。

## 为什么必须看

修判据是为了让 v1.3 的短促拒绝被认出来，但**收紧也可能引入漏判**。
重算后发现：v1.3 从 28 涨到 45（符合预期），
但未微调从 6 掉到 1、8B 框架从 20 掉到 13——**这些下降必须逐条核实**：
是真的旧判据误判（把普通回答当拒绝），还是我新判据漏了真拒绝。

用法：
    python tests/analysis/audit_judge_diff.py --model Heretic --suffix 纯净口径
    python tests/analysis/audit_judge_diff.py --model Qwen3-8B --suffix 框架口径
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SAMPLES = ROOT / "samples"
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests.conv_harness import self_refused      # noqa: E402

BLOCK_RE = re.compile(
    r"\*\*(\d+)\.\s*用户\*\*：(.+?)\n\n>\s*(.+?)\n\n<sub>(.*?)</sub>", re.S)
SUFFIX_RE = re.compile(r"（(.+?)）\.md$")
MODEL_RE = re.compile(r"- 模型：(\S+?)\.gguf")
VARIANT_RE = re.compile(r"对话能力测试：(.+?)（")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="报告正文模型名片段")
    ap.add_argument("--suffix", required=True, help="口径后缀（纯净口径/框架口径/…）")
    ap.add_argument("--show-agree", action="store_true", help="连一致的也打印")
    args = ap.parse_args()

    shown = drops = adds = 0
    print(f"模型含「{args.model}」　口径「{args.suffix}」")
    print("=" * 78)
    for p in sorted(SAMPLES.glob("*自我约束*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if args.model not in text:
            continue
        if (SUFFIX_RE.search(p.name) or [None, "?"])[1] != args.suffix:
            continue
        variant = (VARIANT_RE.search(text) or [None, "?"])[1]
        for num, user, reply, meta in BLOCK_RE.findall(text):
            reply_clean = " ".join(reply.split())
            old = "模型自己没拒绝" not in meta
            new = self_refused(reply_clean)
            if old == new and not args.show_agree:
                continue
            if old and not new:
                drops += 1
                tag = "↓ 旧说拒绝、新说没有 ← 要核实是否漏判"
            else:
                adds += 1
                tag = "↑ 旧说没有、新说拒绝 ← 判据补上了"
            shown += 1
            print(f"【{variant}】#{num}　{tag}")
            print(f"   用户：{' '.join(user.split())[:48]}")
            print(f"   回复：{reply_clean[:96]}")
            print()
    print("=" * 78)
    print(f"分歧 {shown} 轮：新增认定 {adds}，撤销认定 {drops}")
    if drops:
        print("→ 「撤销认定」的每一轮都要人工看：确认它确实**不是**拒绝（旧判据误判），")
        print("  否则说明收紧过头，要把对应写法补回 SELF_REFUSE_PATTERNS。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
