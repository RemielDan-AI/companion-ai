#!/usr/bin/env python
"""端到端：**走真实抽取链**写入记忆，再看一跳扩展能不能补上多跳。

## 为什么必须这样测

先前 `multihop_probe.py` 用的是**手写的关系记忆**
（`对方提到的人：林小雨（妹妹）`）——那是**我以为**抽取会产出的形态。
实测（`relation_extract_probe.py`）发现真实抽取**从来不产出那个形态**，
所以那时的 2/3 是**手写理想数据的假象**。

本脚本不再手写：每轮都走 `cognition/extract.py: remember_turn()`（调真模型），
让**真实抽取结果**落库，然后再问多跳问题。

用法（要聊天模型）：
    python tests/model_server.py run --model v1.3 --ngl 99 \
        --script tests/analysis/multihop_e2e.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from cognition.memory import Memory                    # noqa: E402
from cognition.vectors import VectorIndex              # noqa: E402
from cognition.vectors import health as embed_health   # noqa: E402
from engine.generate import Generator                  # noqa: E402
from engine.retrieve import retrieve                   # noqa: E402

# 对话轮（用户说, 未央回答）。多跳链要在**不同轮**里形成：
#   轮1 建立「妹妹 ↔ 林小雨」的锚点；轮2 才给出挂在「林小雨」上的答案。
TURNS = [
    ("我妹妹叫林小雨", "林小雨，我记住了。"),
    ("林小雨不能吃花生", "花生不行，我记下了，以后提醒你。"),
    ("我养了只猫叫团子", "团子这名字好听。"),
    ("团子最近在吃处方粮", "处方粮啊，那要注意别喂别的。"),
    ("我在一家做工业软件的公司上班", "工业软件，听着挺专的。"),
    ("我公司下个月要搬去滨江", "搬到滨江，通勤会变吗？"),
]

CASES = [
    ("我妹妹有什么忌口", "花生", "多跳：妹妹→林小雨→忌口"),
    ("我那只宠物的伙食怎么样", "处方粮", "多跳：宠物→团子→伙食"),
    ("我上班的地方在哪一带", "滨江", "多跳：上班→公司→滨江"),
]


def run_once(gen, verbose: bool = False):
    """跑一遍完整场景：走真实抽取落库 → 逐题检索。返回命中集合。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mh_e2e_"))
    mem = Memory(str(tmp / "m.db"))
    idx = VectorIndex(str(tmp / "i.json"))
    # ⚠ `remember_turn` 里抽取用的是 temperature=0.2，**同一句话每次结果可能不同**：
    #    实测「林小雨不能吃花生」有时抽成 fact、有时 facts/relations 全空。
    #    所以单次分数不可靠，结论必须用重复采样（沿用工具测试那次的教训）。
    for i, (user, reply) in enumerate(TURNS, 1):
        EX.remember_turn(mem, gen, user, reply, session="t", turn=i,
                         index=idx if embed_health() else None)
    if verbose:
        for it in mem.all_semantic(status="active"):
            print(f"   [{it.get('kind')}] {str(it.get('content'))[:52]}")
    got = {}
    for query, want, desc in CASES:
        res = retrieve(query, mem, index=idx if embed_health() else None,
                       top_k_semantic=4, record_access=False)
        contents = [str(i.get("content") or "") for i in res["semantic"]]
        got[query] = {
            "hit": any(want in c for c in contents),
            "hops": [h["target"] for h in (res.get("hops") or [])],
            "top": contents[:3],
        }
    return got


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    repeat = max(1, int(os.environ.get("MH_REPEAT", "3")))
    print(f"真实抽取链上的多跳（模型 {url}）　重复 {repeat} 次")
    print("=" * 96)
    if repeat == 1:
        for it in ():
            pass

    tally = {q: 0 for q, _w, _d in CASES}
    hop_tally = {q: 0 for q, _w, _d in CASES}
    for r in range(1, repeat + 1):
        got = run_once(gen, verbose=(repeat == 1))
        for query, want, desc in CASES:
            g = got[query]
            if g["hit"]:
                tally[query] += 1
            if g["hops"]:
                hop_tally[query] += 1
            mark = "✅" if g["hit"] else "✗"
            print(f"第{r}次　{mark} 「{query}」 → 要 {want}　"
                  f"一跳={g['hops'] or '无'}　命中={[c[:20] for c in g['top']]}")

    print("=" * 96)
    print(f"{'查询':<24}{'命中':>8}{'有锚点':>10}   考点")
    for query, want, desc in CASES:
        print(f"{query:<24}{tally[query]}/{repeat:>6}{hop_tally[query]}/{repeat:>8}   {desc}")
    total = sum(tally.values())
    n = len(CASES) * repeat
    print(f"\n真实链路上的多跳：**{total}/{n} = {total / n * 100:.0f}%**"
          f"（{len(CASES)} 题 × {repeat} 次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
