#!/usr/bin/env python
"""决定性实验：**未央的回答会不会污染记忆抽取**。

## 为什么要问这个

入库链路是 `extract_turn(generator, user_text, reply_text)`，而提示模板是：

    用户说：{user}
    未央回答：{reply}
    请整理这一轮…只写**用户自己说过的内容**…

也就是说：**回答也在提示里**，而规则只是"请别写用户没说的"——**这是靠模型自觉**。
如果回答真的会污染，那么：

    「林小雨不能吃花生」这条记忆到底是谁说的？

就成了一个必须查清的问题。

## 实验设计

**固定用户原话，只换回答**。如果两次的 relations/facts 不同，
就说明回答进了抽取结果（污染确认）。

    A 组回答：含名字的引导（"林小雨，我记住了。"）
    B 组回答：中性的（"嗯，知道了。"）
    C 组回答：**故意编一个名字**（"好的小美，我记住了。"）← 看会不会被吸进用户记忆

C 组最关键：用户**从没说过"小美"**，如果抽取结果里出现小美，
就证明**未央瞎说的名字会写进用户记忆**。

用法：
    python tests/model_server.py run --model v1.3 --ngl 99 \
        --script tests/analysis/reply_contamination_probe.py
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

USER = "我妹妹叫林小雨"

REPLIES = [
    ("A 含名字的引导", "林小雨，我记住了。"),
    ("B 中性", "嗯，知道了。"),
    ("C **故意编个名字**", "好的小美，我记住了。"),   # 用户从没提过「小美」
    ("D 故意编个事实", "她今年二十八，在做设计师对吧？"),  # 用户从没说过这些
]


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    repeat = max(1, int(os.environ.get("PROBE_REPEAT", "2")))
    print(f"固定用户原话：{USER!r}　只换未央的回答　每档跑 {repeat} 次")
    print("=" * 92)
    for label, reply in REPLIES:
        print(f"\n【{label}】未央回答：{reply}")
        for i in range(1, repeat + 1):
            info = EX.extract_turn(gen, USER, reply)
            rels = info.get("relations") or []
            facts = info.get("facts") or []
            # 检查回复里的独有词有没有被吸进结果
            leaked = []
            for token in ("小美", "二十八", "28", "设计师"):
                if token in reply and any(token in str(x) for x in rels + facts):
                    leaked.append(token)
            print(f"  #{i} relations={rels}")
            print(f"     facts    ={[f[:34] for f in facts]}"
                  + (f"　⚠ **回复里的「{'、'.join(leaked)}」被吸进来了**" if leaked else ""))
    print()
    print("=" * 92)
    print("读法：C/D 组里若出现「小美」「二十八」「设计师」——用户从没说过——")
    print("      就说明**未央的回答会污染用户记忆**，而不只是「整理用户原话」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
