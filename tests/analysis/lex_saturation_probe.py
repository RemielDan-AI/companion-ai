"""把"按最大值归一"换成**绝对饱和函数**，看检索有没有变好。

## 病因（2026-10-17 拆分量查出来的）

`engine/retrieve.py` 里：

    normalize(scores) = {k: v / max(scores.values())}

**按本次查询的最大 BM25 分归一**，于是：

    **任何查询里，词法最接近的那一条永远拿到 lex = 1.0**
    → relevance = max(vec, lex*0.9, blended) ≥ **0.900**

实测后果（37 题标尺）：
    问「我家宠物叫什么」　榜首「用户最近没什么特别的」rel **0.900**，目标只 0.479
    问「我胃怎么样」　　　榜首「你说我该怎么办」　　rel **0.900**，目标只 0.648
       —— 不同问题、不同句子，**相关性都是精确的 0.900**，
          因为它只是"本次查询里词法第一"，**绝对有多相关被抹掉了**。

对照向量：余弦是**绝对**的（0.46 就是 0.46），所以靠向量命中的正确条目
反而被这个虚高 0.9 压死。

## 改法：饱和但**保留绝对量级**

    lex = raw / (raw + K)     # 或 1 - exp(-raw/K)

    raw 很小时 → lex 接近 0（词法几乎没对上，就该低）
    raw 很大时 → lex 趋近 1（真的对上了）
    **仍然在 0–1**，仍可和余弦加权相加（原来的目的保住了）

K 是"半饱和点"：raw=K 时 lex=0.5。要扫一遍选。

## 本探针不改生产代码

它在自己的文件里复算一遍 relevance，只替换 lex 那一项，
用来先看"值不值得改"。判据：hit 不降、MRR 与**榜首对**上升。

用法：
    python tests/analysis/alias_vector_run.py   （EMBED_SCRIPT=lex_saturation_probe.py）
"""
from __future__ import annotations

import math
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
from engine.retrieve import (                                # noqa: E402
    LEXICAL_FLOOR, LEXICAL_WEIGHT, _build_bm25, keywords, lexical_score,
)
from retrieval_bench import CASES, DISTRACTORS               # noqa: E402

TOP_K = 4


def main() -> int:
    if not embed_health():
        print("嵌入服务（8081）不在线")
        return 1
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="lexsat_"))
    mem = Memory(str(tmp / "p.db"))
    for _cat, content, _q, _k in CASES:
        mem.upsert_semantic(kind="fact", content=content, confidence=0.7,
                            importance=0.6)
    for content in DISTRACTORS:
        mem.upsert_semantic(kind="fact", content=content, confidence=0.7,
                            importance=0.6)
    idx = VectorIndex(tmp / "i.json")
    idx.add_texts([(f"s{i['id']}", i["content"]) for i in mem.all_semantic()])
    bm = _build_bm25(mem, set())
    items = list(mem.all_semantic())

    from cognition.vectors import embed
    from cognition import decay as decay_engine
    rules = decay_engine.load_rules()

    def evaluate(lex_fn, label: str) -> dict:
        hit = top1 = 0
        rr = 0.0
        for _cat, _content, query, expect in CASES:
            qg = keywords(query, set())
            raw = dict(bm.search(qg, top_k=500))
            v = embed([query])[0]
            vs = {str(k): float(s) for k, s in idx.search_vector(v, top_k=200)}
            scored = []
            for it in items:
                key = f"s{it['id']}"
                r = raw.get(key, 0.0)
                lex = lex_fn(r)
                # 与生产一致：非零就给保底；再和旧口径取大
                if r > 0 or lex > 0:
                    lex = max(lex, LEXICAL_FLOOR)
                lex = max(lex, lexical_score(qg, it["content"]))
                lex = min(1.0, lex)
                vec = vs.get(key, 0.0)
                blended = LEXICAL_WEIGHT * lex + (1 - LEXICAL_WEIGHT) * vec
                rel = max(vec, lex * 0.9, blended)
                scored.append((decay_engine.rank_score(it, rel, rules), rel, it))
            scored.sort(key=lambda t: (-t[0], -t[1]))
            got = [it["content"] for _s, _r, it in scored[:TOP_K]]
            rank = next((i + 1 for i, g in enumerate(got) if expect in g), 0)
            hit += bool(rank)
            top1 += bool(rank == 1)
            rr += (1.0 / rank) if rank else 0.0
        n = len(CASES)
        return {"label": label, "hit": hit, "n": n, "mrr": rr / n, "top1": top1}

    def norm_lex(raw: float) -> float:
        # 复刻现状：按本次查询最大分归一（这里用单条无法知道全局最大，
        # 由 evaluate 外层的 normalize 语义近似 —— 直接用 raw/max 的等价物：
        # 见下 main 里对"现状"的单独处理）
        return raw

    results = []

    # ---- 现状：按本次查询最大值归一 ----
    def evaluate_current() -> dict:
        hit = top1 = 0
        rr = 0.0
        for _cat, _content, query, expect in CASES:
            qg = keywords(query, set())
            raw = dict(bm.search(qg, top_k=500))
            top = max(raw.values()) if raw else 0.0
            v = embed([query])[0]
            vs = {str(k): float(s) for k, s in idx.search_vector(v, top_k=200)}
            scored = []
            for it in items:
                key = f"s{it['id']}"
                r = raw.get(key, 0.0)
                lex = (r / top) if top > 0 else 0.0
                if r > 0:
                    lex = max(lex, LEXICAL_FLOOR)
                lex = max(lex, lexical_score(qg, it["content"]))
                lex = min(1.0, lex)
                vec = vs.get(key, 0.0)
                blended = LEXICAL_WEIGHT * lex + (1 - LEXICAL_WEIGHT) * vec
                rel = max(vec, lex * 0.9, blended)
                scored.append((decay_engine.rank_score(it, rel, rules), rel, it))
            scored.sort(key=lambda t: (-t[0], -t[1]))
            got = [it["content"] for _s, _r, it in scored[:TOP_K]]
            rank = next((i + 1 for i, g in enumerate(got) if expect in g), 0)
            hit += bool(rank)
            top1 += bool(rank == 1)
            rr += (1.0 / rank) if rank else 0.0
        n = len(CASES)
        return {"label": "现状（按最大归一）", "hit": hit, "n": n,
                "mrr": rr / n, "top1": top1}

    results.append(evaluate_current())
    for K in (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 50.0):
        results.append(evaluate(lambda r, k=K: r / (r + k),
                                f"饱和 K={K}（raw/(raw+K)）"))
    for K in (0.5, 1.0, 2.0, 3.0, 5.0):
        results.append(evaluate(lambda r, k=K: 1 - math.exp(-r / k),
                                f"指数 K={K}（1-e^(-raw/K)）"))
    # ⚠ 极端对照：**纯向量**（lex 恒为 0）。
    #   K 越大 lex 越小，若 K=50 还和 K=5 一样好，说明**词法那一支基本没用**，
    #   那就不该只调 K，而该重新想"词法到底该占多少"。
    results.append(evaluate(lambda r: 0.0, "**纯向量**（lex=0）"))

    print("把「按最大值归一」换成**绝对饱和**：")
    print("=" * 96)
    print(f"{'方案':<26}{'hit@4':>9}{'MRR':>9}{'榜首对':>9}")
    print("-" * 96)
    for r in results:
        print(f"{r['label']:<26}{r['hit']:>5}/{r['n']:<3}{r['mrr']:>9.3f}"
              f"{r['top1']:>6}/{r['n']:<3}")
    print()
    best = max(results, key=lambda r: (r["top1"], r["mrr"]))
    print(f"  榜首对最高：**{best['label']}**（{best['top1']}/{best['n']}，"
          f"MRR {best['mrr']:.3f}）")
    cur = results[0]
    print()
    print("判读：")
    print("  · 现状榜首对偏低就是因为那个虚高 0.9；换成绝对饱和后若榜首对明显涨、"
          "hit 不降 → 值得改")
    print("  · K 是半饱和点（raw=K 时 lex=0.5）。K 太小学不回绝对量级，"
          "K 太大把真实命中压得太狠。")
    mem.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
