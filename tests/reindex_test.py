"""钉住「索引回填」：库里有、索引里没有的条目必须能补上。

## 为什么要它

`cognition/extract.py:297` 把 `index.add_texts()` 的异常**全吞掉**（合理：嵌入挂了
不该影响对话），但**没有回填机制** → 嵌入服务关闭期间写入的记忆**永远不会被向量化**。
实测缺口：库里 8 条、索引只有 3 条（`e3`–`e7` 没索引）。

## 怎么做到不依赖嵌入服务

用**桩替换 `cognition.vectors.embed`**（返回定长假向量），
所以本测试**不连模型、不起服务**，跑得飞快且稳定。
另外单独验「服务不可用时必须立刻返回、不抛异常」。

## 覆盖

    ① pending_items 能准确找出缺失（键的约定与检索一致）
    ② dry_run 只报数不写盘
    ③ 真实回填能把缺口补齐、补完复查为 0
    ④ 嵌入服务不可用 → action=no_embed，**不抛异常、不逐条重试**
    ⑤ 索引已最新 → action=nothing
    ⑥ `spawn_background` 不抛异常
    ⑦ **构造 `Companion` 不起回填线程**（和自动拉起那条同样重要）
    ⑧ 原子落盘：save 之后能完整读回（不是半截文件）
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cognition.vectors as V                        # noqa: E402
from cognition.memory import Memory                  # noqa: E402
from cognition import reindex as RX                  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def make_memory(n_sem: int = 2, n_epi: int = 3) -> Memory:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="reindex_test_"))
    mem = Memory(str(tmp / "m.db"))
    # ⚠ 内容必须**明显不同**：`upsert_semantic` 会把近似重复的事实合并，
    # 写成「用户的事实0/1」只差一个字 → 被并成一条（实测踩过，条数对不上）。
    facts = ["用户对芒果过敏", "用户养了一只叫团子的橘猫",
             "用户喜欢秋天看落叶", "用户在工业软件公司上班"]
    for i in range(n_sem):
        mem.upsert_semantic(kind="fact", content=facts[i % len(facts)],
                            confidence=0.7, importance=0.6)
    for i in range(n_epi):
        mem.add_episode(user_text=f"用户说第{i}件事", reply_text=f"答复第{i}种",
                        summary=f"摘要第{i}类", session="s", turn=i)
    return mem


def main() -> int:
    real_embed = V.embed
    real_health = RX.embed_health

    print("=" * 74)
    print("① pending_items 准确找出缺失（键约定与检索一致）")
    print("=" * 74)
    mem = make_memory(2, 3)
    idx = V.VectorIndex(str(pathlib.Path(tempfile.mkdtemp()) / "i.json"))
    todo = RX.pending_items(mem, idx)
    keys = sorted(k for k, _ in todo)
    check(len(todo) == 5, "5 条都缺", f"实际 {len(todo)}")
    check("s1" in keys and "e3" in keys, "键是 s{id}/e{id}", f"{keys}")

    # 情景那三条要把 summary+user_text+reply_text 都拼上（只索引 summary 会漏名字）
    epi_text = dict(todo).get("e1", "")
    check("用户说第0" in epi_text or "答复第0" in epi_text,
          "情景文本含 user_text/reply_text（不只有 summary）", f"{epi_text[:30]!r}")

    print()
    print("=" * 74)
    print("② dry_run 只报数不写盘")
    print("=" * 74)
    before = len(idx)
    res = RX.backfill_index(mem, idx, dry_run=True)
    check(res["action"] == "dry_run", "action=dry_run")
    check(res["missing"] == 5 and res["added"] == 0, "报缺 5、补 0")
    check(len(idx) == before, "索引条数没变（没写盘）", f"{len(idx)}")

    print()
    print("=" * 74)
    print("③ 真实回填把缺口补齐（用桩 embed，不连服务）")
    print("=" * 74)
    calls = {"n": 0}

    def fake_embed(texts, url=None, timeout=60):
        calls["n"] += 1
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    V.embed = fake_embed
    RX.embed_health = lambda *a, **k: True
    try:
        res = RX.backfill_index(mem, idx, verbose=False)
        check(res["action"] == "done", "action=done", res["detail"])
        check(res["added"] == 5, "补了 5 条", f"实际 {res['added']}")
        check(res["remaining"] == 0, "剩余 0")
        check(len(idx) == 5, "索引条数 5", f"实际 {len(idx)}")
        check(len(RX.pending_items(mem, idx)) == 0, "复查缺口为 0")
        check(res["failed"] == 0, "没有失败")

        print()
        print("=" * 74)
        print("⑤ 索引已最新 → nothing（幂等，不重复烧算力）")
        print("=" * 74)
        n_before = calls["n"]
        res2 = RX.backfill_index(mem, idx, verbose=False)
        check(res2["action"] == "nothing", "action=nothing", res2["detail"])
        check(calls["n"] == n_before, "没有再次调用 embed（幂等）")
    finally:
        V.embed = real_embed
        RX.embed_health = real_health

    print()
    print("=" * 74)
    print("④ 嵌入服务不可用 → 立刻返回 no_embed，不抛异常、不逐条重试")
    print("=" * 74)
    mem2 = make_memory(3, 3)
    idx2 = V.VectorIndex(str(pathlib.Path(tempfile.mkdtemp()) / "i2.json"))
    tried = {"n": 0}

    def boom(*a, **k):
        tried["n"] += 1
        raise AssertionError("服务不可用时不该尝试 embed")

    V.embed = boom
    RX.embed_health = lambda *a, **k: False
    try:
        res3 = RX.backfill_index(mem2, idx2, verbose=False)
        check(res3["action"] == "no_embed", "action=no_embed", res3["detail"])
        check(tried["n"] == 0, "一条都没试着向量化（不逐条重试）")
        check(res3["missing"] == 6, "如实报出缺 6 条", f"实际 {res3['missing']}")
        check(res3["ok"] is False, "ok=False（调用方据此决定要不要提示）")
    finally:
        V.embed = real_embed
        RX.embed_health = real_health

    print()
    print("=" * 74)
    print("⑥ spawn_background 不抛异常，且不阻塞调用方")
    print("=" * 74)
    V.embed = fake_embed
    RX.embed_health = lambda *a, **k: True
    try:
        t0 = time.time()
        RX.spawn_background(mem2, idx2, limit=10, verbose=False)
        elapsed = time.time() - t0
        check(elapsed < 1.0, "立刻返回（后台线程）", f"{elapsed:.2f}s")
    finally:
        V.embed = real_embed
        RX.embed_health = real_health

    print()
    print("=" * 74)
    print("⑦ 构造 Companion **不起**回填线程")
    print("=" * 74)
    import app as A
    before = {t.name for t in threading.enumerate()}
    A.Companion()
    after = {t.name for t in threading.enumerate()}
    # ⚠ 要比**新增**的线程，不是"名字在不在 after 里"：
    #    前面 ⑥ 自己 spawn 过 `index-backfill`，残留线程会让绝对判断误报
    #    （实测踩过）。构造 Companion 本身不该新增任何线程。
    new_threads = after - before
    check("index-backfill" not in new_threads,
          "构造 Companion 没有新增回填线程", f"新增 {sorted(new_threads)}")

    print()
    print("=" * 74)
    print("⑧ 原子落盘：save 后能完整读回")
    print("=" * 74)
    p = pathlib.Path(tempfile.mkdtemp()) / "atomic.json"
    i3 = V.VectorIndex(str(p))
    i3.add("x1", [1.0, 0.0])
    i3.add("x2", [0.0, 1.0])
    i3.save()
    i4 = V.VectorIndex(str(p))
    check(len(i4) == 2 and i4.keys() == {"x1", "x2"}, "读回完整", f"{sorted(i4.keys())}")
    check(not p.with_suffix(p.suffix + ".tmp").exists(), "临时文件已清理")

    print()
    print("=" * 74)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
