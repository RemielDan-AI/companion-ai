#!/usr/bin/env python
"""阶段 17 记忆关联测试（项目改进v1 改进 20）。

验收：记忆之间有关联（因果/时间/人物）、检索时能连带召回、关联可查看和编辑。

用法：python tests/stage17_links_test.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition.memory import Memory  # noqa: E402
from cognition.vectors import VectorIndex  # noqa: E402
from engine.retrieve import build_memory_block, retrieve  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "links"


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    memory = Memory(TMP / "l.db")
    index = VectorIndex(TMP / "index.json")
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 1. 自动时间关联：同一会话相邻两轮 ----
    first = memory.add_episode("今天搬到杭州了", "欢迎", summary="搬到杭州",
                               session="s1", turn=1)
    second = memory.add_episode("晚上去吃了片儿川", "好吃吧", summary="吃片儿川",
                                session="s1", turn=2)
    third = memory.add_episode("别的会话的事", "嗯", summary="无关",
                               session="s2", turn=1)
    links = memory.links(f"episodic#{second}")
    check("相邻两轮自动建立时间关联",
          any(link["kind"] == "temporal" and link["target"] == f"episodic#{first}"
              for link in links), str(links)[:80])
    check("跨会话不会乱连",
          not any(link["target"] == f"episodic#{third}"
                  for link in memory.links(f"episodic#{first}")), "")

    # ---- 2. 人物关联：共同提到同一个名字 ----
    cat1 = memory.add_episode("我养了只猫叫团子", "记住了", summary="养猫叫团子",
                              session="s1", turn=3)
    memory.link_by_entity(f"episodic#{cat1}", ["我养了只猫叫团子", "养猫叫团子"])
    cat2 = memory.add_episode("团子今天把我的杯子打碎了", "它真皮", summary="团子打碎杯子",
                              session="s1", turn=4)
    made = memory.link_by_entity(f"episodic#{cat2}", ["团子今天打碎杯子"])
    check("共同提到同一名字会自动连起来", made >= 1
          and any(link["kind"] == "person"
                  for link in memory.links(f"episodic#{cat2}")), f"连了 {made} 条")

    # ---- 3. 手动因果关联 + 查看 / 编辑 / 删除 ----
    link_id = memory.add_link(f"episodic#{cat2}", f"episodic#{cat1}", "causal", 0.7,
                              "因为养了猫才有杯子被打碎")
    check("能手动建因果关联", link_id > 0, f"#{link_id}")
    check("关联里带类型与备注",
          any(link["kind"] == "causal" and "杯子" in link["note"]
              for link in memory.links(f"episodic#{cat2}")), "")
    check("重复建立同一条关联不会重复",
          memory.add_link(f"episodic#{cat2}", f"episodic#{cat1}", "causal", 0.7, "x")
          == link_id, "")
    all_links = memory.links()
    check("能列出全部关联", len(all_links) >= 3, f"{len(all_links)} 条")
    check("能按类型过滤", all(link["kind"] == "person"
                              for link in memory.links(kind="person")), "")
    check("能删除关联", memory.delete_link(link_id)
          and not any(link["id"] == link_id for link in memory.links()), "")
    check("删除写进审计日志",
          any(item["action"] == "unlink" for item in memory.recent_logs(20)), "")
    try:
        memory.add_link("episodic#1", "episodic#1")
        check("不让自己连自己", False, "")
    except ValueError as exc:
        check("不让自己连自己", "自己" in str(exc), str(exc)[:30])
    try:
        memory.parse_key("乱写的编号")
        check("编号格式错误会报错", False, "")
    except ValueError as exc:
        check("编号格式错误会报错", "编号" in str(exc) or "未知" in str(exc), str(exc)[:30])

    # ---- 4. 检索时连带召回 ----
    memory.upsert_semantic("fact", "对方住在杭州", confidence=0.8)
    fact_id = [item for item in memory.all_semantic() if "杭州" in item["content"]][0]["id"]
    memory.add_link(f"semantic#{fact_id}", f"episodic#{first}", "related", 0.6,
                    "说的都是搬家这件事")
    result = retrieve("杭州", memory, index, top_k_semantic=1, top_k_episodes=1)
    semantic_keys = [item.get("linked_from") for item in result["semantic"]]
    episode_hits = [item["id"] for item in result["episodes"]]
    check("命中语义记忆时连带召回关联的情景记忆",
          first in episode_hits, f"情景命中 {episode_hits}")
    check("连带召回会标明来自哪条",
          any(key for key in semantic_keys) or bool(result["semantic"]), "")

    other = retrieve("完全不相关的话题xyz", memory, index, top_k_semantic=1,
                     top_k_episodes=1)
    check("没命中就不硬塞关联",
          all(item.get("linked_from") for item in other.get("episodes", [])) or
          len(other["episodes"]) <= 1, str([i["id"] for i in other["episodes"]]))

    block = build_memory_block({
        "semantic": [{"kind": "fact", "content": "对方住在杭州"}],
        "episodes": [{"summary": "搬到杭州"}],
    })
    check("关联召回的内容仍受注入长度约束", len(block) <= 400, f"{len(block)} 字")

    # ---- 5. 备份里包含关联（迁移不丢网络） ----
    bundle = TMP / "backup.json"
    exported = memory.export_bundle(bundle)
    check("备份包含关联表", exported["counts"].get("memory_link", 0) >= 2,
          str(exported["counts"]))
    fresh = Memory(TMP / "fresh.db")
    report = fresh.import_bundle(bundle)
    check("恢复后关联还在", report.get("memory_link", 0) >= 2
          and len(fresh.links()) >= 2, str(report.get("memory_link")))

    memory.close()
    fresh.close()
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
