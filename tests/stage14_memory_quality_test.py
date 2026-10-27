#!/usr/bin/env python
"""阶段 14 记忆质量测试：情绪标签（改进 21）+ 相似记忆去重合并（改进 17）。

用法：python tests/stage14_memory_quality_test.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition.memory import Memory  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "quality"


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    memory = Memory(TMP / "q.db")
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 1. 情绪标签（改进 21） ----
    memory.add_episode("今天被领导骂了", "我在", summary="被骂", emotion="难过", turn=1)
    memory.upsert_semantic("fact", "对方养了一只猫叫团子", confidence=0.7, emotion="开心")
    memory.upsert_semantic("fact", "对方住在杭州", confidence=0.6)
    fact = memory.all_semantic(kind="fact")
    tagged = {item["content"]: item["emotion"] for item in fact}
    check("语义记忆会带情绪标签",
          tagged.get("对方养了一只猫叫团子") == "开心", str(tagged))
    check("没情绪的条目情绪为空", tagged.get("对方住在杭州") == "", "")
    check("能按情绪过滤语义记忆",
          len(memory.all_semantic(kind="fact", emotion="开心")) == 1, "")
    stats = memory.emotion_stats()
    check("情绪分布能统计出来",
          stats.get("难过") == 1 and stats.get("开心") == 1, str(stats))
    check("能按情绪取回记忆",
          any("猫" in (item.get("summary") or item.get("content") or "")
              for item in memory.by_emotion("开心")), "")
    memory.set_emotion("episodic", 1, "委屈")
    check("情绪标签可手动纠正",
          memory.entry("episodic", 1)["emotion"] == "委屈", "")
    check("纠正也写进日志",
          any("情绪标签" in str(item.get("detail", ""))
              for item in memory.recent_logs(10)), "")

    # 重复出现时保留已有情绪、也能补上新情绪
    memory.upsert_semantic("fact", "对方养了一只猫叫团子", confidence=0.7, emotion="")
    same = [item for item in memory.all_semantic(kind="fact")
            if item["content"] == "对方养了一只猫叫团子"][0]
    check("重复写入不会把已有情绪冲掉", same["emotion"] == "开心", same["emotion"])
    memory.upsert_semantic("fact", "对方住在杭州", confidence=0.6, emotion="平静")
    hz = [item for item in memory.all_semantic(kind="fact")
          if item["content"] == "对方住在杭州"][0]
    check("原本没情绪的条目不补上情绪", hz["emotion"] == "平静", hz["emotion"])

    # ---- 2. 相似记忆去重合并（改进 17） ----
    memory.upsert_semantic("preference", "对方喜欢喝美式咖啡", confidence=0.6)
    memory.upsert_semantic("preference", "对方喜欢喝美式咖啡，不加糖", confidence=0.5)
    memory.upsert_semantic("preference", "对方喜欢跑步", confidence=0.5)
    preview = memory.dedupe_semantic(threshold=0.6, dry_run=True)
    check("预演能找出相似记忆", len(preview) == 1 and "美式" in preview[0]["content"],
          str(preview)[:60])
    check("预演不改数据", memory.semantic_count() == 5, str(memory.semantic_count()))

    merged = memory.dedupe_semantic(threshold=0.6)
    check("真的合并了相似记忆", len(merged) == 1, str(merged))
    prefs = memory.all_semantic(kind="preference")
    check("合并后只剩更完整的那条",
          len(prefs) == 2 and any("不加糖" in item["content"] for item in prefs),
          "、".join(item["content"] for item in prefs))
    kept = [item for item in prefs if "不加糖" in item["content"]][0]
    check("证据数累加（出现次数可查）", int(kept["evidence_count"]) >= 2,
          str(kept["evidence_count"]))
    check("没有被合并掉的活跃条目", memory.semantic_count("active") == 4,
          str(memory.semantic_count("active")))
    dropped = [item for item in memory.all_semantic(status="merged")]
    check("被合并的条目标记为 merged 并留了指向",
          len(dropped) == 1 and int(dropped[0]["merged_into"]) == kept["id"],
          str(dropped[0]["merged_into"]) if dropped else "无")
    check("合并动作写进审计日志",
          any(item["action"] == "merge" for item in memory.recent_logs(20)), "")

    again = memory.dedupe_semantic(threshold=0.6)
    check("重复跑合并不会重复合并", not again, str(again))

    memory.close()
    shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"\n通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
