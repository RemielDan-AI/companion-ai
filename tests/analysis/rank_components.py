"""拆开看：榜首被干扰句抢走时，**是干扰句分太高，还是目标分太低**？

## 为什么还要查这一层

`info_score_probe.py` 量出"惩罚低信息量"这个方向**不成立**
（加了惩罚 MRR 反而从 0.776 掉到 0.742、榜首对还少一个）。
但"信号不好用"和"病因判断错了"是两件事——

如果真实情况是**目标条目的向量分本来就低**，那问题在**嵌入**（or 查询改写），
惩罚干扰句根本不对症，改了也是白改。

所以要逐个分量看清楚：
    vec      向量相似度
    lex      BM25 词法分
    legacy   旧口径（二字组交集比）
    relevance = max(vec, 0.9*lex, blended)   ← 最终相关度
    score    relevance 再乘重要度/新鲜度衰减 → 排序就按它

用法：
    python tests/analysis/alias_vector_run.py   （EMBED_SCRIPT=rank_components.py）
"""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(HERE))

import tempfile                                              # noqa: E402

from cognition.memory import Memory                          # noqa: E402
from cognition.vectors import VectorIndex, health as embed_health  # noqa: E402
from engine import retrieve as R                             # noqa: E402
from retrieval_bench import CASES, DISTRACTORS               # noqa: E402

# 这几道在标尺里"榜首被干扰句抢走"
FOCUS = [
    "我家宠物叫什么",
    "我胃怎么样",
    "我最爱什么季节",
    "我有什么忌口",
    "我最近在练什么",
]


def main() -> int:
    if not embed_health():
        print("嵌入服务（8081）不在线")
        return 1
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rankcomp_"))
    mem = Memory(str(tmp / "p.db"))
    for _cat, content, _q, _k in CASES:
        mem.upsert_semantic(kind="fact", content=content, confidence=0.7,
                            importance=0.6)
    for content in DISTRACTORS:
        mem.upsert_semantic(kind="fact", content=content, confidence=0.7,
                            importance=0.6)
    idx = VectorIndex(tmp / "i.json")
    idx.add_texts([(f"s{i['id']}", i["content"]) for i in mem.all_semantic()])

    expect_of = {q: k for _c, _content, q, k in CASES}
    rules = __import__("cognition.decay", fromlist=["x"]).load_rules()

    print("拆分量：榜首是干扰句时，各方分量长什么样")
    print("=" * 100)
    for query in FOCUS:
        expect = expect_of.get(query, "?")
        res = R.retrieve(query, mem, index=idx, top_k_semantic=6,
                         rules=rules, record_access=False)
        got = res.get("semantic") or []
        print(f"\n问「{query}」　目标：{expect}")
        print(f"  {'':<4}{'内容':<34}{'vec':>7}{'lex':>7}{'rel':>7}{'score':>8}  标记")
        for i, it in enumerate(got[:5], 1):
            rel = float(it.get("relevance") or 0)
            sc = float(it.get("score") or 0)
            hit = " ← **目标**" if expect in it["content"] else ""
            print(f"  {i:<4}{it['content'][:32]:<34}{'':>7}{'':>7}"
                  f"{rel:>7.3f}{sc:>8.3f}{hit}")
        print(f"  （vec/lex 需要底层打分，这里只看 relevance 与最终 score）")

    print()
    print("=" * 100)
    print("另外：把目标的 relevance 和榜首的 relevance 直接比一比")
    print("=" * 100)
    gaps = []
    for _c, _content, query, expect in CASES:
        res = R.retrieve(query, mem, index=idx, top_k_semantic=60,
                         rules=rules, record_access=False)
        got = res.get("semantic") or []
        if not got:
            continue
        top = float(got[0].get("relevance") or 0)
        tgt = next((float(it.get("relevance") or 0) for it in got
                    if expect in it["content"]), None)
        if tgt is None:
            continue
        gaps.append((query, top, tgt))
    worse = [(q, t, g) for q, t, g in gaps if g < t]
    print(f"  有目标条目但 relevance 低于榜首的：{len(worse)}/{len(gaps)}")
    for q, t, g in worse[:8]:
        print(f"    {q:<22} 榜首 {t:.3f}　目标 {g:.3f}　差 {g-t:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
