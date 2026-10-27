#!/usr/bin/env python
"""阶段 19 记忆抽象与聚类测试（项目改进v1 改进 19）。

验收：能从具体事件提炼模式、聚类成主题、作为高层记忆、减少碎片化、可追溯到原始记忆。

用法：python tests/stage19_theme_test.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition import themes as themes_engine  # noqa: E402
from cognition.memory import Memory  # noqa: E402
from engine.retrieve import build_memory_block, retrieve  # noqa: E402
from cognition.vectors import VectorIndex  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "themes"


class FakeGenerator:
    """假模型：固定回一句模式。"""

    def complete(self, system, user, max_tokens=400, temperature=0.2):
        return "他常提到工作压力大，晚上睡不好。"


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    memory = Memory(TMP / "t.db")
    index = VectorIndex(TMP / "index.json")
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # 造三组：一组关于"工作压力大"（措辞相近，够大），两条一组（不够大），几条孤立的
    for text in ("工作压力大，睡不着", "工作压力大，很累", "工作压力大，睡不好",
                 "今天工作压力大"):
        memory.add_episode(text, "我在", summary=text, session="s", turn=1, emotion="疲惫")
    memory.add_episode("周末去爬山了", "好", summary="周末去爬山", session="s", turn=2)
    memory.add_episode("爬山看到云海", "真好", summary="爬山看到云海", session="s", turn=3)
    memory.add_episode("新买了键盘", "不错", summary="新买了键盘", session="s", turn=4)

    # ---- 1. 聚类 ----
    groups = themes_engine.cluster(memory, threshold=0.22, min_size=3)
    check("相似记忆被聚成一组", len(groups) == 1 and len(groups[0]) == 4,
          f"{len(groups)} 组，最大 {len(groups[0]) if groups else 0} 条")
    small = themes_engine.cluster(memory, threshold=0.22, min_size=5)
    check("成员不够多的组不建主题", not small, f"{len(small)} 组")
    check("标题取的是共同片段",
          "压力" in themes_engine._title(groups[0]), themes_engine._title(groups[0]))

    # ---- 2. 提炼模式：模板兜底 + 模型路径 ----
    text, source = themes_engine._pattern(groups[0])
    check("没有模型时用模板句兜底", source == "template" and "反复出现" in text, text[:30])
    text2, source2 = themes_engine._pattern(groups[0], FakeGenerator())
    check("有模型时让模型提炼", source2 == "model" and "工作压力" in text2, text2[:30])

    # ---- 3. 落库成为高层记忆 ----
    saved = themes_engine.build(memory, FakeGenerator(), threshold=0.22, min_size=3)
    check("主题被存下来", len(saved) == 1 and saved[0]["count"] == 4, str(saved))
    listed = themes_engine.themes(memory)
    check("能列出高层记忆",
          len(listed) == 1 and listed[0]["source"] == "model"
          and listed[0]["count"] == 4, str(listed)[:80])
    check("重复构建不会建重复主题",
          len(themes_engine.build(memory, FakeGenerator(), threshold=0.22, min_size=3)) == 1
          and len(themes_engine.themes(memory)) == 1, "")

    # ---- 4. 可追溯：成员能取回原文 ----
    trace = themes_engine.trace(memory, listed[0]["id"])
    check("能追溯到原始记忆",
          len(trace) == 4 and all(item["text"] for item in trace),
          str([item["key"] for item in trace])[:60])
    check("成员编号写成了 episodic#N 形式",
          all(str(item["key"]).startswith("episodic#") for item in trace), "")

    # ---- 5. 检索时作为高层记忆注入 ----
    result = retrieve("我最近工作压力大吗", memory, index)
    theme_hits = [item for item in result["semantic"] if item.get("theme")]
    check("相关主题会作为高层记忆被召回", bool(theme_hits),
          str([item.get("title") for item in theme_hits])[:40])
    block = build_memory_block(result)
    check("注入块里用「长期印象」单列，不混进事实",
          "长期印象" in block or not theme_hits, block[:80])
    check("注入块仍在长度预算内", len(block) <= 400, f"{len(block)} 字")
    unrelated = retrieve("今天想吃什么", memory, index)
    check("不相关时不硬塞主题",
          not [item for item in unrelated["semantic"] if item.get("theme")], "")

    # ---- 6. 减少碎片化：主题数远少于原始条数 ----
    check("抽象确实压缩了碎片",
          len(themes_engine.themes(memory)) < len(themes_engine.collect(memory)), "")

    # ---- 7. 删除主题 ----
    check("能删掉主题", themes_engine.drop(memory, listed[0]["id"])
          and not themes_engine.themes(memory), "")
    check("删除写进审计日志",
          any(item["action"] == "drop" for item in memory.recent_logs(10)), "")
    check("抽象动作也写日志",
          any(item["action"] == "abstract" for item in memory.recent_logs(20)), "")

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
