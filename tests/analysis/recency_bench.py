"""量"**近期聊的应该排前面**"这件事，并扫排序权重。

## 现状

`rank_score = 0.60×相关性 + 0.25×重要度 + 0.15×新鲜度`
`recency_score = 0.5 ** (距今天数 / 半衰期)`，默认半衰期 **60 天**。

**问题**：60 天半衰期下，"昨天"和"一周前"分别是 0.99 和 0.92——
**近期与近期之间的先后几乎分不出来**，只有跨月的差别才明显。
而用户要的是"**越近期聊的内容越排前**"（同一件事前后说过两次，该信近的那次）。

## 本探针量两件事

    ① 两对同义正、只有时间不同的记忆：现在**近的那条排第一吗**？
    ② 扫 `(relevance, importance, recency)` 权重，看**近的排第一**能到多少，
       同时**相关性**（目标能不能进 top4）会不会被牺牲。

⚠ 关键约束：**相关性必须仍是主导**。`rank_score` 只用来给"已经过了闸门的候选"排序，
  如果把 recency 抬太高，会把"近期但不相关"的顶上来。
  所以判据是：**近期命中率上升，且 hit@4 不降**。

用法：
    python tests/analysis/alias_vector_run.py   （EMBED_SCRIPT=recency_bench.py）
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import decay as decay_engine                # noqa: E402
from cognition.memory import Memory                        # noqa: E402
from cognition.vectors import VectorIndex, health as embed_health   # noqa: E402
from engine import retrieve as R                           # noqa: E402

TOP_K = 4

# (旧说法, 新说法, 问法, 期望命中的关键词, 旧说法几天前)
# 「同一件事前后说过两次，近的那次该排前面」
PAIRS = [
    ("用户住在南京", "用户搬到成都了", "我住在哪", "成都", 40),
    ("用户喜欢喝美式", "用户现在喜欢喝拿铁", "我喜欢喝什么", "拿铁", 30),
    ("用户在银行上班", "用户换到学校上班", "我在哪上班", "学校", 90),
    ("用户的猫叫团子", "用户的猫改名叫汤圆", "我的猫叫什么", "汤圆", 20),
    ("用户对花生过敏", "用户现在对花生不过敏了", "我对什么过敏", "花生", 60),
    ("用户想学吉他", "用户已经开始学尤克里里", "我在学什么乐器", "尤克里里", 15),
]

# 互不相干的干扰句（用来看"抬 recency 会不会把无关的顶上来"）
DISTRACTORS = [
    "用户今天有点累", "用户说还行", "用户最近没什么特别的",
    "用户问了句吃了没", "用户说天气不错", "用户随口提了一句",
]

# (relevance, importance, recency)
WEIGHTS = [
    (0.60, 0.25, 0.15),   # 现状
    (0.55, 0.20, 0.25),
    (0.50, 0.20, 0.30),
    (0.45, 0.15, 0.40),
    (0.40, 0.15, 0.45),
    (0.50, 0.10, 0.40),
]


def _stamp(days_ago: float) -> str:
    t = dt.datetime.now() - dt.timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    if not embed_health():
        print("嵌入服务（8081）不在线")
        return 1
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="recency_"))
    mem = Memory(str(tmp / "b.db"))

    # 旧的先写、新的后写，并**手工回拨时间**（不用等真时间）
    for old, new, _q, _k, days in PAIRS:
        sid, _ = mem.upsert_semantic(kind="fact", content=old, confidence=0.7,
                                     importance=0.6)
        mem.conn.execute(
            "UPDATE semantic SET created_at=?, updated_at=?, last_access_at='' WHERE id=?",
            (_stamp(days + 1), _stamp(days + 1), sid))
        sid2, _ = mem.upsert_semantic(kind="fact", content=new, confidence=0.7,
                                      importance=0.6)
        mem.conn.execute(
            "UPDATE semantic SET created_at=?, updated_at=?, last_access_at='' WHERE id=?",
            (_stamp(1), _stamp(1), sid2))
    for d in DISTRACTORS:
        sid, _ = mem.upsert_semantic(kind="fact", content=d, confidence=0.7,
                                     importance=0.6)
        mem.conn.execute(
            "UPDATE semantic SET created_at=?, updated_at=?, last_access_at='' WHERE id=?",
            (_stamp(1), _stamp(1), sid))
    mem.conn.commit()

    idx = VectorIndex(tmp / "i.json")
    idx.add_texts([(f"s{it['id']}", it["content"]) for it in mem.all_semantic()])

    base = decay_engine.load_rules()
    print("「近期聊的排前」：同一件事新旧两条并存时，近的那条排第几？")
    print("=" * 96)
    print(f"{'权重(rel/imp/rec)':<22}{'近期排第一':>12}{'目标进top4':>12}{'旧说法排前':>12}")
    print("-" * 96)
    best = None
    for w in WEIGHTS:
        rules = dict(base)
        rules["rank_weights"] = {"relevance": w[0], "importance": w[1], "recency": w[2]}
        recent_first = old_first = hit = 0
        for old, new, q, key, _days in PAIRS:
            res = R.retrieve(q, mem, index=idx, top_k_semantic=TOP_K,
                             rules=rules, record_access=False)
            got = [it["content"] for it in (res.get("semantic") or [])]
            pos_new = next((i for i, g in enumerate(got) if new in g), None)
            pos_old = next((i for i, g in enumerate(got) if old in g), None)
            if pos_new is not None and (pos_old is None or pos_new < pos_old):
                recent_first += 1
            if pos_old is not None and (pos_new is None or pos_old < pos_new):
                old_first += 1
            if any(key in g for g in got):
                hit += 1
        n = len(PAIRS)
        label = f"{w[0]}/{w[1]}/{w[2]}"
        print(f"{label:<22}{recent_first:>8}/{n:<3}{hit:>9}/{n:<3}{old_first:>9}/{n:<3}")
        if best is None or (recent_first, hit) > (best[0], best[1]):
            best = (recent_first, hit, w)

    print()
    print(f"  最优：近期排前 {best[0]}/{len(PAIRS)}　hit {best[1]}/{len(PAIRS)}"
          f"　权重 {best[2]}")
    print()
    print("判读：")
    print("  · 「近期排第一」越多越好；「旧说法排前」应当越少越好（那是反的）")
    print("  · **hit 不能降**：抬 recency 不能把「近期但不相关」的顶上来")
    print("  · 现状 0.60/0.25/0.15 会是多少，是本次要对比的基准")
    mem.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
