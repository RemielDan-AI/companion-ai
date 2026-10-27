#!/usr/bin/env python
"""阶段 2 冒烟测试：不需要大模型，几秒钟跑完。

覆盖两条容易被忽略的路径：
  1. 反馈记录（点赞/点踩/纠正）能否正确落库
  2. 嵌入服务不可用时，检索能否退化为关键词匹配而不报错

用法：python tests/smoke_stage2.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition.memory import Memory  # noqa: E402
from cognition.vectors import EmbeddingUnavailable, VectorIndex, embed  # noqa: E402
from engine.retrieve import build_memory_block, retrieve  # noqa: E402
from feedback import collector  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp"


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    db = TMP / "smoke.db"
    # 用独立的空向量索引：绝不能读真实索引（否则用户聊过之后这个测试就没意义了）
    index_file = TMP / "smoke_index.json"
    for path in (db, index_file):
        if path.exists():
            path.unlink()
    index = VectorIndex(index_file)

    memory = Memory(db)
    failures: list[str] = []

    # ---- 1. 反馈 ----
    memory.add_episode("我养了只猫叫团子", "团子真可爱", summary="聊到宠物猫",
                       emotion="开心", importance=2, turn=1)
    collector.record_last(memory, "up", "这句感觉很好")
    collector.record_last(memory, "down")
    collector.record_last(memory, "correction", "团子是橘猫")
    stats = collector.summarize(memory)
    print(f"反馈统计：{stats}")
    if "点赞 1 次" not in stats or "点踩 1 次" not in stats or "纠正 1 次" not in stats:
        failures.append("反馈统计不正确")
    if collector.last_episode_id(memory) != 1:
        failures.append("取最近一轮 id 不正确")
    try:
        collector.record(memory, "unknown")
        failures.append("非法反馈类型没有被拒绝")
    except ValueError:
        print("非法反馈类型被正确拒绝 ✓")

    # ---- 2. 嵌入服务不可用时的关键词兜底 ----
    bad_url = "http://127.0.0.1:59999/v1/embeddings"
    try:
        embed(["测试"], url=bad_url, timeout=2)
        failures.append("嵌入服务不可用却没有抛 EmbeddingUnavailable")
    except EmbeddingUnavailable:
        print("嵌入失败能抛出 EmbeddingUnavailable ✓")

    result = retrieve("我家猫叫什么", memory, index=index)
    print(f"兜底检索模式：{result['mode']}，情景命中 {len(result['episodes'])} 条")
    for item in result["episodes"]:
        print(f"  #{item['id']} {item['summary']}（{item['score']}）")
    if result["mode"] != "keyword":
        failures.append("兜底检索没有走关键词模式")
    if not result["episodes"]:
        failures.append("关键词兜底没有命中任何情景记忆")
    block = build_memory_block(result)
    print(f"注入块：{block.replace(chr(10), ' / ')}")

    # ---- 3. 语义记忆去重升权 ----
    _, created_first = memory.upsert_semantic("fact", "用户养了一只猫")
    _, created_again = memory.upsert_semantic("fact", "用户养了一只猫。")
    evidence = memory.all_semantic()[0]["evidence_count"]
    print(f"语义去重：首次新建={created_first}，重复写入新建={created_again}，证据数={evidence}")
    if not created_first or created_again or evidence != 2:
        failures.append("语义记忆去重/升权不正确")

    # ---- 清除记忆（调试用） ----
    before = memory.stats()
    counts = memory.clear()
    after = memory.stats()
    index_cleared = index.clear()
    if after["episodes"] or after["semantic"] or len(index):
        failures.append("清除记忆后还有残留")
    # 自增序号也应重置，调试时重新从 #1 开始
    new_id = memory.add_episode("清空后再记一条", "好", summary="验证序号", turn=1)
    print(f"清除记忆：情景 {before['episodes']} 轮／语义 {before['semantic']} 条"
          f"／向量 {index_cleared} 条 -> 清后 {after['episodes']}／{after['semantic']}"
          f"，新记录 id={new_id}")
    if new_id != 1:
        failures.append(f"清除后自增序号没重置（拿到 id={new_id}）")

    memory.close()
    shutil.rmtree(TMP, ignore_errors=True)

    print()
    if failures:
        for item in failures:
            print(f"✗ {item}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
