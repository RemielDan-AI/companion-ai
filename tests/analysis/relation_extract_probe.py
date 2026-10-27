#!/usr/bin/env python
"""实测：真实抽取器对**机构/单位类**句子产出什么关系（决定一跳锚点够不够）。

## 为什么要实测

多跳缺口的三条里，第 3 条（`我上班的地方在哪一带` → 滨江）补不上。
我先前归因成「抽取没把公司存成关系记忆」——但那是**推断**，得看真模型吐什么。

## 一跳锚点的格式要求

`engine/retrieve.py: relation_targets()` 靠的是关系记忆的 `名字（关系）` 形态：

    cognition/extract.py:137   →  "林小雨（妹妹）"      ← 从"叫XX"绑定名字
    cognition/extract.py:156   →  "对方提到的人：林小雨（妹妹）"

**没有括号的关系（如「对方提到的人：公司」）取不到锚点**，
所以要么让抽取产出带关系词的括号形态，要么在 `relation_targets` 里另想办法。

用法（要聊天模型）：
    python tests/model_server.py run --model v1.3 --ngl 99 \
        --script tests/analysis/relation_extract_probe.py
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

# (用户说, 未央回答)　覆盖：人名 / 机构 / 地点 / 宠物 / 无「叫」的绑定
CASES = [
    ("我妹妹叫林小雨", "嗯，林小雨，我记住了。"),
    ("我妹妹在读大学", "嗯，她在读大学啊，那应该挺忙的。"),
    ("我在一家做工业软件的公司上班", "工业软件啊，听起来挺有意思的。"),
    ("我公司下个月要搬去滨江", "搬到滨江啊，通勤会变吗？"),
    ("我养了只猫叫团子", "团子！这名字好听。"),
    ("我每天早上八点上班", "作息挺规律的。"),
]


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"抽取器实际产出（模型服务 {url}）")
    print("=" * 88)
    for user, reply in CASES:
        try:
            info = EX.extract_turn(gen, user, reply)
        except Exception as exc:
            print(f"「{user}」✗ {type(exc).__name__}: {exc}")
            continue
        rels = info.get("relations") or []
        facts = info.get("facts") or []
        print(f"用户：{user}")
        print(f"  relations = {rels}")
        print(f"  facts     = {[f[:30] for f in facts]}")
        # 逐条走一遍真实落库形态（_canonical 会加前缀，_enrich_relation 会绑名字）
        for r in rels:
            enriched = EX._enrich_relation(r, user)
            canon = EX._canonical("relation", enriched)
            has_alias = "（" in canon and "）" in canon
            mark = "✅ 有锚点" if has_alias else "✗ **没有括号 → 取不到一跳锚点**"
            print(f"    落库：{canon!r}　{mark}")
        print("-" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
