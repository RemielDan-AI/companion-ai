#!/usr/bin/env python
"""BM25 与融合逻辑的单元测试（不依赖嵌入服务、不碰真实数据）。

验收：
1. BM25 的 IDF 确实把「高频字组」压低、把「稀有字组」抬高；
2. 长度归一化生效（同样词频下，长文档得分不高于短文档）；
3. 归一化按最大值、融合公式等于 `0.6*bm25 + 0.4*vector`；
4. **无向量候选不丢**（单边分数也保留）；
5. 持久化往返一致；
6. 拿一个「高频词干扰」的构造语料，验证 BM25 把目标条排上去而旧的
   「二字组交集比」排不上（这是本次改动的直接动机）。

用法：python tests/bm25_test.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.bm25 import BM25, fuse, normalize  # noqa: E402
from engine.retrieve import keywords  # noqa: E402

FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '✗'} {name}" + (f"　{detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


def main() -> int:
    print("== 1. BM25 基本量与 IDF ==")
    bm = BM25()
    docs = {
        "d1": "用户对芒果过敏",
        "d2": "我今天很累",
        "d3": "我觉得这样挺好",
        "d4": "我是不是很没用",
        "d5": "我最近压力有点大",
    }
    for k, v in docs.items():
        bm.add(k, keywords(v))
    check("文档数", bm.n == 5, f"n={bm.n}")
    check("平均长度 > 0", bm.avg_len > 0, f"avg={bm.avg_len:.2f}")
    # ⚠ 上面这 5 条语料里**每个二字组 df 都是 1**（实测），IDF 全等于 1.386、
    #    完全没有区分度。这正是「BM25 需要足够大语料」的实证。
    #    所以要验 IDF 是否真起作用，必须**自己构造可控词频的语料**。
    bm_ctl = BM25()
    # "最近" 出现在 4 篇里（高频）；"压力" 只在 1 篇里（稀有）
    bm_ctl.add("c1", ["最近", "压力"])
    bm_ctl.add("c2", ["最近", "天气"])
    bm_ctl.add("c3", ["最近", "加班"])
    bm_ctl.add("c4", ["最近", "吃饭"])
    bm_ctl.add("c5", ["无关"])
    check("高频字组 df 更高", bm_ctl.df["最近"] == 4 and bm_ctl.df["压力"] == 1,
          f"最近 df={bm_ctl.df['最近']}, 压力 df={bm_ctl.df['压力']}")
    check("稀有字组 IDF > 高频字组 IDF",
          bm_ctl.idf("压力") > bm_ctl.idf("最近"),
          f"压力={bm_ctl.idf('压力'):.3f} > 最近={bm_ctl.idf('最近'):.3f}（"
          f"比值 {bm_ctl.idf('压力')/bm_ctl.idf('最近'):.2f}×）")
    check("IDF 恒为正（Lucene 平滑）", all(bm.idf(t) > 0 for t in bm.df),
          f"最小={min(bm.idf(t) for t in bm.df):.4f}")

    print("\n== 2. 检索排序：目标条排第一 ==")
    hits = bm.search(keywords("我对什么过敏"), top_k=3)
    check("「过敏」问到 d1", hits and hits[0][0] == "d1",
          f"top3={[h[0] for h in hits]}")

    print("\n== 3. 长度归一化 ==")
    bm2 = BM25()
    bm2.add("short", keywords("芒果"))
    bm2.add("long", keywords("芒果" + "然后" * 40))
    s_short = bm2.score(keywords("芒果"), "short")
    s_long = bm2.score(keywords("芒果"), "long")
    check("同词频下长文档得分更低", s_long < s_short,
          f"long={s_long:.4f} < short={s_short:.4f}")

    print("\n== 4. 归一化与融合 ==")
    norm = normalize({"a": 4.0, "b": 2.0})
    check("按最大值归一", norm == {"a": 1.0, "b": 0.5}, str(norm))
    check("空输入不炸", normalize({}) == {})
    fused = fuse({"a": 1.0}, {"a": 1.0, "b": 1.0}, sparse_weight=0.6)
    check("融合值 = 0.6*bm + 0.4*vec",
          abs(fused["a"] - 1.0) < 1e-9 and abs(fused["b"] - 0.6) < 1e-9,
          str({k: round(v, 3) for k, v in fused.items()}))
    check("无向量候选不丢（b 只有 bm25 分也在）", "b" in fused)
    only_vec = fuse({"z": 3.0}, {}, sparse_weight=0.6)
    check("无词法候选不丢", only_vec.get("z") == 0.4, str(only_vec))

    print("\n== 5. 持久化往返 ==")
    tmp = pathlib.Path(__file__).resolve().parent / "_tmp" / "bm25.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    bm.save(tmp)
    back = BM25.load(tmp)
    check("文档数一致", back.n == bm.n, f"{back.n} vs {bm.n}")
    check("df 重建正确", dict(back.df) == dict(bm.df))
    check("打分一致",
          abs(back.score(keywords("我对什么过敏"), "d1")
              - bm.score(keywords("我对什么过敏"), "d1")) < 1e-9)

    print("== 6. 对比旧口径：没有 IDF 会排错 ==")

    def old_lexical(query: str, text: str) -> float:
        """旧口径：二字组交集 ÷ **查询**字组数（无 IDF），复刻 retrieve.lexical_score。

        注意它的分母是查询、不是文档，所以**长文档并不占便宜**——
        它真正的毛病是**每个字组同权**：命中一个到处都有的高频字组，
        和命中一个只此一家的主题字组，贡献完全一样。
        """
        qg = keywords(query)
        tg = keywords(text)
        if not qg or not tg:
            return 0.0
        return len(qg & tg) / max(1, len(qg))

    # 查询里有两个字组：`压力`(稀有、真主题) 与 `最近`(高频、几乎所有记忆都有)
    query = "压力"
    # A 文档只讲到"压力"（真相关）；B 文档只讲到"最近"（不相关，但"最近"到处都是）
    docs_ctl = {
        "A_只讲压力": "压力",
        "B_只讲最近": "最近",
        "x1": "最近",
        "x2": "最近",
        "x3": "最近",
    }
    bm_ctl2 = BM25()
    for k, v in docs_ctl.items():
        bm_ctl2.add(k, keywords(v))
    hits_ctl = [d for d, _ in bm_ctl2.search(keywords(query), top_k=5)]
    print(f"     语料：A_只讲压力 / B_只讲最近 / 另有 3 条也含「最近」")
    print(f"     df：压力={bm_ctl2.df.get('压力', 0)}　最近={bm_ctl2.df.get('最近', 0)}")
    print(f"     BM25 顺序：{hits_ctl}")
    check("BM25 把「稀有主题词」那条排第一", hits_ctl and hits_ctl[0] == "A_只讲压力",
          f"top1={hits_ctl[0] if hits_ctl else None}")
    # B 只含高频词"最近"、不含主题词"压力" → BM25 给它 0 分直接不进结果。
    # 这比"排在后面"更彻底，两种都算对。
    if "B_只讲最近" in hits_ctl:
        check("含高频词的 B 被排到后面",
              hits_ctl.index("B_只讲最近") > 0,
              f"B 位置={hits_ctl.index('B_只讲最近')}")
    else:
        check("含高频词的 B 得 0 分、被排除（比排后面更彻底）", True,
              f"结果只有 {hits_ctl}，B 不在其中")

    # 同一个查询下，旧口径对「只命中高频词」的 B 给了非零分（分不出它其实不相关）
    old_a = old_lexical("压力 最近", "压力")
    old_b = old_lexical("压力 最近", "最近")
    print(f"     旧口径（查询='压力 最近'）：A={old_a:.3f}　B={old_b:.3f}")
    check("旧口径给「只命中高频词」的 B 非零分（无法区分主题词）", old_b > 0,
          f"B={old_b:.3f}（B 其实与主题无关，只因含高频词「最近」而得分）")
    # 用 IDF 加权后，B 的贡献被压到远低于 A
    idf_a = old_a * bm_ctl2.idf("压力")
    idf_b = old_b * bm_ctl2.idf("最近")
    print(f"     IDF 加权后：A={idf_a:.4f}　B={idf_b:.4f}（比值 {idf_a/idf_b:.1f}×）")
    check("IDF 加权后 A 远高于 B", idf_a > idf_b * 3,
          f"{idf_a:.4f} vs {idf_b:.4f}")

    print()
    if FAIL:
        print(f"✗ {len(FAIL)} 项未通过：" + "、".join(FAIL))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
