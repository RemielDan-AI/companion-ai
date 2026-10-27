"""找"低信息量"的**可算信号**：哪个真的能把干扰句和真事实分开？

## 问题（2026-10-17 标尺实测）

37 道题里**有 7 道（19%）的第 1 名是个干扰句**：

    我家宠物叫什么   → 第1是「用户最近没什么特别的」
    我胃怎么样       → 第1是「你说我该怎么办」
    我最爱什么季节   → 第1是「我最近压力有点大」

这是因为泛泛的句子在嵌入空间里**靠近"中心"**，跟谁的相似度都不低。
IDF 管住了**词法**那一侧，**向量那一侧没有任何机制**做这件事。

## 候选信号（都要能程序侧算出来）

    ① **平均 IDF**：条目的词平均有多稀有
       ⚠ 但 `cognition/bm25.py` 自己的注释就写着：**语料小时 df 几乎全是 1，
         IDF 退化成常数**。本项目 bench 只有 52 条文档 → 这个信号在这里**可能量不出来**。
         生产库里条目多得多，可能有效。要**分开看**。
    ② **离质心的余弦**：条目向量与全库平均向量的相似度
       —— 直接对着"泛句靠近中心"这个病因，而且**不受语料大小影响**。
    ③ **虚词占比**：「用户最近没什么特别的」这类句子虚字多
       —— 最粗糙，但零依赖。

## 量什么

    · 干扰句 vs 真事实，两个信号各自的**均值/范围/重叠**
    · 若加一个**有界惩罚**（`score *= 0.6 + 0.4*info`，最多压 40%），
      **榜首变对了吗**、**hit@4/MRR 掉了吗**
      ⚠ 惩罚必须**有界**：无界会把"短但真实"的记忆也砍掉（那是丢记忆）

用法：
    python tests/analysis/alias_vector_run.py   （EMBED_SCRIPT=info_score_probe.py）
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
from cognition.vectors import embed, health as embed_health  # noqa: E402
from engine import retrieve as R                             # noqa: E402
from retrieval_bench import CASES, DISTRACTORS               # noqa: E402

# 虚词：一个二元组两个字都是虚词时，它不承载信息
_STOP = set("的了呢吗吧啊哦嗯我你他她它们这那是有在也就都还又和跟对把被给从着过会很太太")


def _content_ratio(text: str) -> float:
    """实义二元组占全部二元组的比例（虚词占比的补）。"""
    t = str(text or "")
    grams = [t[i:i + 2] for i in range(len(t) - 1)
             if all("\u4e00" <= ch <= "\u9fff" for ch in t[i:i + 2])]
    if not grams:
        return 1.0
    content = [g for g in grams if not all(ch in _STOP for ch in g)]
    return len(content) / len(grams)


def _cos(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / (na * nb) if na and nb else 0.0


def _report(name: str, target_vals: list[float], noise_vals: list[float],
            higher_means_noisy: bool) -> float:
    """打印一个信号的分离度，返回"重叠区间"占全幅的比例（越小越好）。"""
    t_lo, t_hi = min(target_vals), max(target_vals)
    n_lo, n_hi = min(noise_vals), max(noise_vals)
    overlap = max(0.0, min(t_hi, n_hi) - max(t_lo, n_lo))
    span = max(t_hi, n_hi) - min(t_lo, n_lo)
    print(f"\n  {name}")
    print(f"    真事实：均值 {sum(target_vals)/len(target_vals):.3f}"
          f"　范围 {t_lo:.3f}~{t_hi:.3f}")
    print(f"    干扰句：均值 {sum(noise_vals)/len(noise_vals):.3f}"
          f"　范围 {n_lo:.3f}~{n_hi:.3f}")
    if higher_means_noisy:
        print(f"    → 干扰句应该**更高**；实际 "
              f"{'✅ 对' if sum(noise_vals)/len(noise_vals) > sum(target_vals)/len(target_vals) else '✗ 反了'}")
    else:
        print(f"    → 干扰句应该**更低**；实际 "
              f"{'✅ 对' if sum(noise_vals)/len(noise_vals) < sum(target_vals)/len(target_vals) else '✗ 反了'}")
    print(f"    重叠区间占幅：{(overlap/span if span else 0):.0%}"
          f"（{'分得开' if span and overlap/span < 0.35 else '**重叠严重**'}）")
    return (overlap / span) if span else 1.0


def main() -> int:
    if not embed_health():
        print("嵌入服务（8081）不在线")
        return 1

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="infoprobe_"))
    mem = Memory(str(tmp / "p.db"))
    targets, noises = [], []
    for _cat, content, _q, _k in CASES:
        iid, _ = mem.upsert_semantic(kind="fact", content=content,
                                     confidence=0.7, importance=0.6)
        targets.append((iid, content))
    for content in DISTRACTORS:
        iid, _ = mem.upsert_semantic(kind="fact", content=content,
                                     confidence=0.7, importance=0.6)
        noises.append((iid, content))

    from cognition.vectors import VectorIndex
    idx = VectorIndex(tmp / "i.json")
    pairs = [(f"s{i['id']}", i["content"]) for i in mem.all_semantic()]
    idx.add_texts(pairs)

    # ---- 信号③ 虚词占比 ----
    c_ratio_t = [_content_ratio(c) for _i, c in targets]
    c_ratio_n = [_content_ratio(c) for _i, c in noises]

    # ---- 信号① 平均 IDF ----
    from cognition.bm25 import BM25
    from engine.retrieve import keywords
    bm = BM25()
    for it in mem.all_semantic():
        bm.add(f"s{it['id']}", keywords(str(it["content"]), set()))

    def mean_idf(content: str) -> float:
        terms = keywords(content, set())
        vals = [bm.idf(t) for t in terms]
        return sum(vals) / len(vals) if vals else 0.0

    i_t = [mean_idf(c) for _i, c in targets]
    i_n = [mean_idf(c) for _i, c in noises]

    # ---- 信号② 离质心的余弦 ----
    vecs = embed([c for _i, c in targets] + [c for _i, c in noises])
    dim = len(vecs[0])
    centroid = [sum(v[d] for v in vecs) / len(vecs) for d in range(dim)]
    c_t = [_cos(v, centroid) for v in vecs[:len(targets)]]
    c_n = [_cos(v, centroid) for v in vecs[len(targets):]]

    print("低信息量信号：能不能把干扰句和真事实分开？")
    print("=" * 90)
    print(f"  真事实 {len(targets)} 条　干扰句 {len(noises)} 条")
    _report("① 平均 IDF（越高越有信息）", i_t, i_n, higher_means_noisy=False)
    _report("② 离质心的余弦（越高越泛）", c_t, c_n, higher_means_noisy=True)
    _report("③ 实义二元组占比（越高越有信息）", c_ratio_t, c_ratio_n,
            higher_means_noisy=False)

    # ---- 加有界惩罚后，榜首/命中怎么变 ----
    print()
    print("=" * 90)
    print("加**有界惩罚**之后再检索（score *= 0.6 + 0.4*info）：")
    print("=" * 90)

    def norm(vals: list[float]) -> tuple[float, float]:
        return min(vals), max(vals)

    def penalty_centroid(content: str) -> float:
        v = embed([content])[0]
        cos = _cos(v, centroid)
        lo, hi = norm(c_t + c_n)
        info = 1.0 - (cos - lo) / (hi - lo) if hi > lo else 1.0
        return 0.6 + 0.4 * max(0.0, min(1.0, info))

    rules = __import__("cognition.decay", fromlist=["x"]).load_rules()
    base_hit = base_top1 = 0
    pen_hit = pen_top1 = 0
    base_rr = pen_rr = 0.0
    for _cat, _content, query, expect in CASES:
        res = R.retrieve(query, mem, index=idx, top_k_semantic=60,
                         rules=rules, record_access=False)
        got = [dict(it) for it in (res.get("semantic") or [])]
        if not got:
            continue
        r0 = next((i + 1 for i, g in enumerate(got) if expect in g["content"]), 0)
        base_hit += bool(r0)
        base_top1 += bool(r0 == 1)
        base_rr += (1.0 / r0) if r0 else 0.0
        for it in got:
            it["_adj"] = float(it.get("score") or 0) * penalty_centroid(it["content"])
        got.sort(key=lambda it: -it["_adj"])
        r1 = next((i + 1 for i, g in enumerate(got) if expect in g["content"]), 0)
        pen_hit += bool(r1)
        pen_top1 += bool(r1 == 1)
        pen_rr += (1.0 / r1) if r1 else 0.0

    n = len(CASES)
    print(f"  {'':<10}{'hit':>8}{'MRR':>9}{'榜首对':>9}")
    print(f"  {'原始':<10}{base_hit:>5}/{n:<3}{base_rr/n:>9.3f}{base_top1:>6}/{n:<3}")
    print(f"  {'加惩罚':<10}{pen_hit:>5}/{n:<3}{pen_rr/n:>9.3f}{pen_top1:>6}/{n:<3}")
    print()
    print("  判读：")
    print("    · 榜首对**涨了**、hit/MRR **没掉** → 这个信号值得接")
    print("    · hit 掉了 → 惩罚伤到了真事实，得收紧（或改用更弱的惩罚系数）")
    print("    · 三样都没变 → 信号没有判别力，别接")
    mem.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
