# -*- coding: utf-8 -*-
"""BM25：给检索补上「有 IDF 的词法分」。

## 为什么要它

`engine/retrieve.py` 原来的 `lexical_score` 是
「查询二字组 ∩ 文档二字组 ÷ 查询二字组数」——**没有 IDF**。
于是「我/的/是」这类几乎每条记忆都含的高频字组，与「芒果/议程/吉他」这类
有区分度的字组**同权**。实测（`tests/analysis/retrieval_bench.py`，15 条语料）：
带向量 8/10，纯词法 **1/10**（9 题直接返回空，因为词汇分过不了准入闸门）。

BM25 用 IDF 把高频字组压下去、把稀有字组抬起来，正是这个缺口的对症药。

## 参数来源

照 VCPToolBox（`project-other/VCPToolBox/Plugin/RAGDiaryPlugin/DirectDiaryTextProcessor.js:18-57`）：
k1=1.5、b=0.75、IDF 用 Lucene 平滑式 `log(1 + (N - df + 0.5)/(df + 0.5))`。

## 重要：IDF 需要足够大的语料到才能发挥作用

本项目的 bench 只有十几条文档，此时 `df` 几乎全是 1，**IDF 退化成常数、BM25 ≈ 词频加权**，
体现不出优势。**语料上到几百条后 BM25 的收益才真实**。
所以不要用那个小 bench 论证"BM25 有效"，它只能验证"融合逻辑通了"。

## 设计取舍

- 纯 Python、零依赖（不引 rank_bm25 / jieba）。切词复用 `retrieve.keywords`，
  与现有检索**同一套分词**——否则词法分和向量分的候选集会对不上。
- 索引可落盘缓存（`dict` ↔ JSON），因为 BM25 的 `df` 要扫全语料。
- 增量更新走 `add()`；`remove()` 只扣计数、不重算，`rebuild()` 才彻底重来。
"""
from __future__ import annotations

import json
import math
import pathlib
from collections import Counter

K1 = 1.5
B = 0.75


class BM25:
    """极简 BM25 索引。

    docs: doc_id -> {term: tf}
    df:   term -> 含该词的文档数
    """

    def __init__(self) -> None:
        self.docs: dict[str, Counter[str]] = {}
        self.lengths: dict[str, int] = {}
        self.df: Counter[str] = Counter()

    # ---------------------------------------------------------------- 建索引
    def add(self, doc_id: str, terms) -> None:
        """加入/覆盖一篇文档。`terms` 是已切好的词集合或列表。"""
        doc_id = str(doc_id)
        if doc_id in self.docs:
            self.remove(doc_id)
        counter = Counter(terms)
        if not counter:
            return
        self.docs[doc_id] = counter
        self.lengths[doc_id] = sum(counter.values())
        for term in counter:
            self.df[term] += 1

    def remove(self, doc_id: str) -> None:
        doc_id = str(doc_id)
        counter = self.docs.pop(doc_id, None)
        if not counter:
            return
        self.lengths.pop(doc_id, None)
        for term in counter:
            if term in self.df:
                self.df[term] -= 1
                if self.df[term] <= 0:
                    del self.df[term]

    def rebuild(self, rows: list[tuple[str, object]]) -> None:
        """整体重建：rows = [(doc_id, terms), ...]"""
        self.docs.clear()
        self.lengths.clear()
        self.df.clear()
        for doc_id, terms in rows:
            self.add(doc_id, terms)

    # ---------------------------------------------------------------- 打分
    @property
    def n(self) -> int:
        return len(self.docs)

    def __len__(self) -> int:
        return len(self.docs)

    @property
    def avg_len(self) -> float:
        if not self.docs:
            return 0.0
        return sum(self.lengths.values()) / len(self.docs)

    def idf(self, term: str) -> float:
        """Lucene 平滑式，恒为正，避免 df>N/2 时出现负分。"""
        if not self.n:
            return 0.0
        df = self.df.get(term, 0)
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, query_terms, doc_id: str) -> float:
        counter = self.docs.get(str(doc_id))
        if not counter:
            return 0.0
        avg = self.avg_len or 1.0
        length = self.lengths.get(str(doc_id), 0)
        total = 0.0
        for term in set(query_terms or ()):
            tf = counter.get(term, 0)
            if not tf:
                continue
            denom = tf + K1 * (1 - B + B * length / avg)
            total += self.idf(term) * (tf * (K1 + 1)) / denom
        return total

    def search(self, query_terms, top_k: int = 50) -> list[tuple[str, float]]:
        """给所有文档打分并取前 top_k。文档几千条以内暴力足够快。"""
        scored = [(doc_id, self.score(query_terms, doc_id)) for doc_id in self.docs]
        scored = [(d, s) for d, s in scored if s > 0]
        scored.sort(key=lambda item: -item[1])
        return scored[:top_k]

    # ---------------------------------------------------------------- 持久化
    def to_json(self) -> dict:
        return {
            "k1": K1, "b": B,
            "docs": {k: dict(v) for k, v in self.docs.items()},
            "lengths": self.lengths,
        }

    @classmethod
    def from_json(cls, data: dict) -> "BM25":
        obj = cls()
        for doc_id, counter in (data.get("docs") or {}).items():
            obj.docs[str(doc_id)] = Counter(counter)
            obj.lengths[str(doc_id)] = sum(counter.values())
        for counter in obj.docs.values():
            for term in counter:
                obj.df[term] += 1
        return obj

    def save(self, path: pathlib.Path | str) -> None:
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: pathlib.Path | str) -> "BM25":
        path = pathlib.Path(path)
        if not path.exists():
            return cls()
        try:
            return cls.from_json(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return cls()


def normalize(scores: dict[str, float]) -> dict[str, float]:
    """按最大值归一到 [0,1]（VCPToolBox 也是按每库自身最大值归一，`RAGDiaryPlugin.js:884`）。

    为什么不按总和：这一层要的是「本库里谁最相关」，归一后 BM25 与余弦才可加权相加。
    """
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {k: 0.0 for k in scores}
    return {k: v / top for k, v in scores.items()}


def fuse(vector_scores: dict[str, float], bm25_scores: dict[str, float],
         sparse_weight: float = 0.6) -> dict[str, float]:
    """加权线性融合（照 VCPToolBox 的 sparseWeight，默认 0.6 给词法侧）。

    两边都先按各自最大值归一，再 `sparse_weight * bm25 + (1-sparse_weight) * vector`。
    缺哪一边就当 0——与 `ResultDeduplicator`「无向量候选一律保留」同样的保守取向：
    宁可让它以单边分数进来，也不要静默丢掉。
    """
    sparse_weight = min(1.0, max(0.0, float(sparse_weight)))
    bm = normalize(bm25_scores)
    vec = normalize(vector_scores)
    keys = set(bm) | set(vec)
    return {
        k: sparse_weight * bm.get(k, 0.0) + (1 - sparse_weight) * vec.get(k, 0.0)
        for k in keys
    }
