#!/usr/bin/env python
"""原型：用**已有的关系记忆**做一跳扩展，看能不能补上多跳缺口。

## 背景（先量出来的事实）

`multihop_probe.py` 实测：单跳 **1/1**，多跳 **0/3**。
三条多跳失败时，排第一的**恰好都是第一跳**（入口找对了、走不到答案）。

## 为什么不需要浪潮算法

浪潮算法（TagMemo Wave，VCPToolBox）是**标签共现图**上的有界非回溯传播，
要标签、要累积的共现结构（2600+ 行 JS + Rust 引擎 + 106 个配置项）。
而走这一跳需要的数据我们**已经存着了**：

    cognition/extract.py:137   →  "林小雨（妹妹）"
    cognition/extract.py:156   →  "对方提到的人：林小雨（妹妹）"

即**别名 → 名字**的映射。`engine/retrieve.py: known_names()` 已经在读这个格式了。

## 本原型做什么

    ① 查询里出现关系词（妹妹/宠物/公司）→ 在 relation 记忆里找到对应名字（林小雨/团子）
    ② 拿这些名字再检索一轮
    ③ 按折扣权重并进结果

只做**一跳**，带跳数衰减（借浪潮算法「有限脉冲响应 w_t=γ^t/Σγ^r」的思路，γ=0.6）。
先量它值不值得接线，**不动生产代码**。

用法：
    python tests/analysis/hop_expansion_proto.py
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.memory import Memory                    # noqa: E402
from cognition.vectors import VectorIndex               # noqa: E402
from cognition.vectors import health as embed_health   # noqa: E402
from engine.retrieve import keywords, known_names, retrieve   # noqa: E402

# 借浪潮算法「有限脉冲响应」的跳数衰减：w_t = γ^t / Σγ^r，γ=0.6
GAMMA = 0.6

MEMORIES = [
    "用户妹妹林小雨在读大学",
    "林小雨不能吃花生",
    "用户在工业软件公司上班",
    "那家公司的总部在滨江",
    "用户养的宠物叫团子，是只橘猫",
    "团子最近在吃处方粮",
    # 干扰项
    "用户今天很累，什么都不想干",
    "用户最近压力有点大",
    "用户喜欢秋天，喜欢看落叶",
    "用户对芒果过敏",
    # 关系记忆：别名 → 名字（这是真实抽取的产物，格式见 extract.py:156）
    "对方提到的人：林小雨（妹妹）",
    "对方提到的人：团子（宠物）",
]

CASES = [
    ("我妹妹有什么忌口", "花生", "多跳"),
    ("我上班的地方在哪一带", "滨江", "多跳"),
    ("我那只宠物的伙食怎么样", "处方粮", "多跳"),
    ("我对什么过敏", "芒果", "单跳"),
    ("我妹妹叫什么", "林小雨", "单跳（关系记忆）"),
]


def hop_targets(query: str, memory: Memory, known: set[str]) -> list[str]:
    """从查询里的关系词出发，找出该跳过去的**名字**。

    关系记忆的 content 形如「对方提到的人：林小雨（妹妹）」——
    所以拿查询的词去匹配括号里的关系词，取出括号外的名字。
    """
    qgrams = keywords(query, known)
    out: list[str] = []
    try:
        items = [i for i in memory.all_semantic(status="active")
                 if str(i.get("kind")) == "relation"]
    except Exception:
        return out
    for item in items:
        body = str(item.get("content") or "").replace("对方提到的人：", "")
        for m in re.finditer(r"([\u4e00-\u9fff]{2,4})（([^）]+)）", body):
            name, alias = m.group(1), m.group(2)
            # 查询里出现这个关系词 → 跳过去
            if alias in query or (alias in qgrams) or any(
                    alias in q for q in qgrams):
                if name != alias:
                    out.append(name)
    return list(dict.fromkeys(out))


def retrieve_with_hop(query: str, memory: Memory, index, top_k: int = 4):
    """先常规检索，再从关系词跳一跳，按 γ 衰减合并。"""
    base = retrieve(query, memory, index=index, top_k_semantic=top_k,
                    record_access=False)
    known = known_names(memory)
    targets = hop_targets(query, memory, known)
    merged: dict[str, dict] = {}
    for it in base["semantic"]:
        merged[str(it.get("content"))] = dict(it)
    hop_detail = []
    for t, name in enumerate(targets, 1):
        sub = retrieve(name, memory, index=index, top_k_semantic=top_k,
                       record_access=False)
        w = GAMMA ** t
        got = [str(s.get("content") or "") for s in sub["semantic"]]
        hop_detail.append((name, w, got[:2]))
        for it in sub["semantic"]:
            c = str(it.get("content"))
            hit = dict(it)
            hit["score"] = round(float(it.get("score") or 0) * w, 3)
            hit["relevance"] = round(float(it.get("relevance") or 0) * w, 3)
            hit["_hop"] = name
            if c not in merged:
                merged[c] = hit
    ordered = sorted(merged.values(),
                     key=lambda d: -(float(d.get("score") or 0)))
    return ordered[:top_k], targets, hop_detail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=4)
    args = ap.parse_args()

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="hop_"))
    mem = Memory(str(tmp / "m.db"))
    idx = VectorIndex(str(tmp / "i.json"))
    use_vec = embed_health()
    print(f"隔离库：{tmp}　嵌入服务：{use_vec}")
    print(f"跳数衰减 γ={GAMMA}（借浪潮算法的有限脉冲响应 w_t=γ^t/Σγ^r）\n")

    items = []
    for content in MEMORIES:
        kind = "relation" if content.startswith("对方提到的人") else "fact"
        sid = mem.upsert_semantic(kind=kind, content=content,
                                  confidence=0.7, importance=0.6)
        items.append((f"s{sid}", content))
    if use_vec:
        try:
            idx.add_texts(items)
        except Exception as exc:
            print(f"建索引失败：{exc}")
            use_vec = False

    print("=" * 92)
    print(f"{'类型':<8}{'查询':<20}{'答案':<8}{'基线':<6}{'加一跳':<8}排第一（加一跳后）")
    print("=" * 92)
    stat = {"基线": [0, 0], "加一跳": [0, 0], "多跳基线": [0, 0], "多跳加一跳": [0, 0]}
    for query, want, kind in CASES:
        base = retrieve(query, mem, index=idx if use_vec else None,
                        top_k_semantic=args.top_k, record_access=False)
        bgot = [str(i.get("content") or "") for i in base["semantic"]]
        b_ok = any(want in c for c in bgot)

        hgot, targets, detail = retrieve_with_hop(
            query, mem, idx if use_vec else None, args.top_k)
        hlist = [str(i.get("content") or "") for i in hgot]
        h_ok = any(want in c for c in hlist)

        stat["基线"][1] += 1
        stat["加一跳"][1] += 1
        if b_ok:
            stat["基线"][0] += 1
        if h_ok:
            stat["加一跳"][0] += 1
        if kind.startswith("多跳"):
            stat["多跳基线"][1] += 1
            stat["多跳加一跳"][1] += 1
            if b_ok:
                stat["多跳基线"][0] += 1
            if h_ok:
                stat["多跳加一跳"][0] += 1

        top = hlist[0][:30] if hlist else "（无）"
        print(f"{kind:<8}{query:<20}{want:<8}"
              f"{'✅' if b_ok else '✗':<6}{'✅' if h_ok else '✗':<8}{top}")
        if targets:
            print(f"        └ 跳过去：{targets}；该跳第 2 轮拿到：{[d[2] for d in detail]}")
        else:
            print(f"        └ 没找到可跳的关系词")

    print("=" * 92)
    for label, key in (("全部·基线", "基线"), ("全部·加一跳", "加一跳"),
                       ("**多跳·基线**", "多跳基线"), ("**多跳·加一跳**", "多跳加一跳")):
        ok, n = stat[key]
        if n:
            print(f"  {label}：{ok}/{n} = {ok / n * 100:.0f}%")
    print()
    b, h = stat["多跳基线"], stat["多跳加一跳"]
    if h[0] > b[0]:
        print(f"→ 一跳扩展把多跳从 {b[0]}/{b[1]} 提到 {h[0]}/{h[1]}：" 
              "**用已有数据就能补，不必移植浪潮算法**。")
    else:
        print("→ 一跳扩展没改善，缺口比预想的深（可能要真图传播）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
