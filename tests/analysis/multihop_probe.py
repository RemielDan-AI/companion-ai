#!/usr/bin/env python
"""多跳联想探针：量我们**到底有没有**浪潮算法要解决的那个缺口。

## 为什么要写

VCPToolBox 的「浪潮算法」（TagMemo Wave）是在**标签共现图**上做有界非回溯传播，
目标是「哪些概念在长期记忆中共同出现 / 更可能沿哪条局部关系继续展开」——
也就是**多跳联想**。

而我们现有的检索基准（`retrieval_bench.py`）10 条**全是单跳**事实召回
（「我对什么过敏」→ 芒果），命中 9/10。**单跳不是浪潮算法的战场。**
所以不能拿那个 9/10 说"我们不需要"，也不能凭感觉说"需要"——
得先造出真正的多跳题，看我们现在是过还是不过。

## 多跳是怎么难住检索的

    A: 用户妹妹林小雨在读大学          ← 查询里出现的是「妹妹」
    B: 林小雨对花生过敏                ← 答案挂在「林小雨」上

问「我妹妹对什么过敏」：字面/向量都指向 A（含"妹妹"），
但答案在 B。**必须走 A→B 这一跳**才能拿到。

用法：
    python tests/analysis/multihop_probe.py            # 需要嵌入服务
    python tests/analysis/multihop_probe.py --no-vector
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.memory import Memory                    # noqa: E402
from cognition.vectors import VectorIndex, embed       # noqa: E402
from cognition.vectors import health as embed_health   # noqa: E402
from engine.retrieve import retrieve                   # noqa: E402

# 纯事实记忆（**不含关系锚点**）
# ⚠ 设计要点：**答案那条记忆不能和查询共享任何词**，否则就退化成单跳。
#    第一版我写错了——「我妹妹对什么过敏」配答案「林小雨对花生**过敏**」，
#    共享"过敏"直接字面命中，看着 3/3 全过，其实一条多跳都没测到。
FACTS = [
    # 链 1：妹妹 → 林小雨 → 忌口
    "用户妹妹林小雨在读大学",
    "林小雨不能吃花生",
    # 链 2：上班 → 那家公司 → 滨江
    "用户在工业软件公司上班",
    "那家公司的总部在滨江",
    # 链 3：宠物 → 团子 → 伙食
    "用户养的宠物叫团子，是只橘猫",
    "团子最近在吃处方粮",
    # 干扰项（含高频字）
    "用户今天很累，什么都不想干",
    "用户最近压力有点大",
    "用户喜欢秋天，喜欢看落叶",
    # 单跳对照：查询与答案直接重合
    "用户对芒果过敏",
]

# 关系记忆（**别名 → 名字**）。这是一跳扩展的锚点，也是真实抽取链的产物：
#   cognition/extract.py:137  →  "林小雨（妹妹）"
#   cognition/extract.py:156  →  "对方提到的人：林小雨（妹妹）"
# 两种情形必须分开测：
#   「无锚点」= 只用 FACTS          → 一跳无从下手（这就是原始缺口）
#   「有锚点」= FACTS + RELATIONS   → 一跳能补上
# ⚠ 「公司」**没有**对应关系记忆 —— 不是探针漏写，而是当前抽取的真实缺口
#    （`relations` 的提示词例子是"妹妹、同事小李、妈妈"，不含单位/公司）。
#    所以第 2 条多跳在「有锚点」下也补不上，正好用来暴露这个缺口。
RELATIONS = [
    "对方提到的人：林小雨（妹妹）",
    "对方提到的人：团子（宠物）",
]

CASES = [
    # ---- 真多跳：查询与答案记忆**零词重叠**，只能经中间实体到达 ----
    #      查询词「妹妹/忌口」；答案记忆只有「林小雨/花生/不能吃」
    ("我妹妹有什么忌口", "花生", "妹妹→林小雨→忌口", 2),
    #      查询词「上班/地方/一带」；答案记忆只有「那家公司/总部/滨江」
    ("我上班的地方在哪一带", "滨江", "上班→那家公司→滨江", 2),
    #      查询词「宠物/伙食」；答案记忆只有「团子/处方粮」
    ("我那只宠物的伙食怎么样", "处方粮", "宠物→团子→伙食", 2),
    # ---- 单跳对照 ----
    ("我对什么过敏", "芒果", "单跳对照（查询与答案重合）", 1),
]


def run_scenario(label: str, extra_relations: bool, top_k: int, use_vec: bool):
    """跑一遍：写入记忆 → 逐题检索。返回 {(类型): [命中, 总数]}。

    `extra_relations=True` 时一并写入关系记忆（别名→名字），
    这是一跳扩展的锚点——两种情形都要看，才能分清
    「算法没接上」和「没有锚点可跳」。
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="multihop_"))
    mem = Memory(str(tmp / "m.db"))
    idx = VectorIndex(str(tmp / "i.json"))
    items = []
    for content in FACTS:
        sid = mem.upsert_semantic(kind="fact", content=content,
                                  confidence=0.7, importance=0.6)
        items.append((f"s{sid}", content))
    if extra_relations:
        for content in RELATIONS:
            sid = mem.upsert_semantic(kind="relation", content=content,
                                      confidence=0.8, importance=0.6)
            items.append((f"s{sid}", content))
    if use_vec:
        try:
            idx.add_texts(items)
        except Exception:
            use_vec = False

    print("=" * 96)
    print(f"【{label}】")
    print(f"{'类型':<8}{'查询':<22}{'要的':<8}{'结果':<6}排在最前的命中")
    print("-" * 96)
    hits = {"多跳": [0, 0], "单跳": [0, 0]}
    for query, want, desc, hops in CASES:
        kind = "多跳" if hops >= 2 else "单跳"
        res = retrieve(query, mem, index=idx if use_vec else None,
                       top_k_semantic=top_k, record_access=False)
        got = [str(it.get("content") or "") for it in res["semantic"]]
        rank = next((i + 1 for i, c in enumerate(got) if want in c), None)
        ok = rank is not None
        hits[kind][1] += 1
        if ok:
            hits[kind][0] += 1
        top = got[0][:36] if got else "（无命中）"
        print(f"{kind:<8}{query:<22}{want:<8}{'✅' if ok else '✗':<6}{top}")
        if ok and rank > 1:
            print(f"        └ 命中排第 {rank} 位（排序弱：正确答案没排第一）")
        hops_info = res.get("hops") or []
        if hops_info:
            print(f"        └ 一跳：{[(h['target'], h['weight']) for h in hops_info]}"
                  f" → 该跳第 2 轮拿到 {[h['got'] for h in hops_info]}")
        else:
            print(f"        └ 没跳到（没有可用的关系锚点）")
    print("-" * 96)
    for kind in ("单跳", "多跳"):
        ok, n = hits[kind]
        if n:
            print(f"  {label} · {kind}：{ok}/{n} = {ok / n * 100:.0f}%")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vector", action="store_true")
    ap.add_argument("--top-k", type=int, default=4)
    args = ap.parse_args()

    use_vec = (not args.no_vector) and embed_health()
    print(f"嵌入服务可用：{embed_health()}　→ 本次{'用' if use_vec else '不用'}向量")
    print("对照两种情形：没有关系锚点 vs 有（真实抽取会产出关系记忆）\n")

    a = run_scenario("无锚点（只有事实记忆）", False, args.top_k, use_vec)
    print()
    b = run_scenario("有锚点（事实 + 关系记忆）", True, args.top_k, use_vec)

    print()
    print("=" * 96)
    print("对照")
    print("=" * 96)
    for kind in ("单跳", "多跳"):
        oa, na = a[kind]
        ob, nb = b[kind]
        if na and nb:
            print(f"  {kind}：无锚点 {oa}/{na} → 有锚点 {ob}/{nb}")
    mh_a, mh_b = a["多跳"], b["多跳"]
    print()
    if mh_b[0] > mh_a[0]:
        print(f"→ 一跳扩展生效：多跳 {mh_a[0]}/{mh_a[1]} → {mh_b[0]}/{mh_b[1]}。")
        print("  剩余没补上的那条说明**锚点缺失**（抽取没把「公司」存成关系记忆），"
              "不是算法问题。")
    else:
        print("→ 一跳没生效：要么锚点全缺，要么接线有问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
