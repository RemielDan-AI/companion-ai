#!/usr/bin/env python
"""阶段 20 系统联动测试（项目改进v1 改进 29-31）。

验收：
- 29 价值观证据链存在记忆库、变化可回溯、双向关联；
- 30 技能调用结果可存（日志与记忆分离但可关联）、记忆可触发技能、关联可查看；
- 31 AI 人格档案独立存储、与用户画像互不污染。

用法：python tests/stage20_linkage_test.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition import linkage  # noqa: E402
from cognition.memory import Memory  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "linkage"


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    memory = Memory(TMP / "m.db")
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 改进 30：技能日志与记忆分离但可关联 ----
    ep1 = memory.add_episode("杭州今天天气怎么样", "查查", summary="问天气",
                             session="s", turn=1)
    skill_id = linkage.log_skill(memory, "E001", "local", "weather",
                                 "刚查到的实时信息：杭州现在多云 18℃", True, ep1)
    check("技能调用单独记一条日志", skill_id > 0
          and len(linkage.skill_logs(memory)) == 1, f"#{skill_id}")
    check("技能日志里存了结果", "18℃" in linkage.skill_logs(memory)[0]["result"], "")
    linked = memory.links(f"skill#{skill_id}")
    check("技能日志与那轮记忆关联起来了",
          any(link["kind"] == "skill_use" and link["target"] == f"episodic#{ep1}"
              for link in linked), str(linked)[:70])
    back = linkage.skill_episode(memory, skill_id)
    check("能从技能日志倒查到当时那轮对话",
          back is not None and back["summary"] == "问天气", str(back)[:40])
    check("技能日志与情景记忆是两份数据（分离）",
          len(memory.recent_episodes(10)) == 1 and len(linkage.skill_logs(memory)) == 1, "")

    # 记忆可触发技能：待办就是记忆触发的现成例子
    memory.add_reminder("给妹妹打电话")
    todos = linkage.pending_reminders(memory)
    check("待办（记忆触发技能）能取出来",
          len(todos) == 1 and "妹妹" in todos[0]["content"], str(todos)[:40])

    # ---- 改进 29：价值观证据链 ----
    ep2 = memory.add_episode("我不喜欢别人一直追问", "好，不追问", summary="不喜欢被追问",
                             session="s", turn=2, emotion="烦")
    made = linkage.link_value_evidence(memory, ["V1"], ep2, "第 2 轮对话促成")
    check("价值观与促成它的记忆建立关联", made == 1
          and any(link["kind"] == "evidence" for link in memory.links("value#V1")), "")
    evidence = linkage.value_evidence(memory, "V1")
    check("能列出某个价值观的证据记忆",
          len(evidence) == 1 and evidence[0]["key"] == f"episodic#{ep2}", str(evidence)[:60])
    check("证据里带上了当时的说明", "促成" in (evidence[0]["note"] or ""), evidence[0]["note"])
    check("双向可查（从记忆那边也能查到价值观）",
          any(link["source"] == "value#V1" for link in memory.links(f"episodic#{ep2}")), "")
    check("多个价值观可以指向同一条记忆",
          linkage.link_value_evidence(memory, ["V2", "V3"], ep2) == 2
          and len(linkage.value_evidence(memory, "V2")) == 1, "")

    # ---- 改进 31：人格档案独立 ----
    user_profile = TMP / "profile.json"
    user_profile.write_text('{"facts": [{"content": "用户住在杭州"}]}', encoding="utf-8")
    ai_profile = TMP / "ai_profile.yaml"
    ai_profile.write_text("identity:\n  name: 未央\ntraits:\n  liveliness: 0.6\n",
                          encoding="utf-8")
    separation = linkage.persona_separation(user_profile, ai_profile)
    check("用户画像与 AI 档案是两个文件",
          separation["separated"] and separation["user_profile"] != separation["ai_profile"],
          separation["detail"])
    ai_profile.write_text("identity:\n  name: 未央\n", encoding="utf-8")
    check("改 AI 档案不影响用户画像",
          "杭州" in user_profile.read_text(encoding="utf-8"), "")
    same = linkage.persona_separation(user_profile, user_profile)
    check("指向同一文件时会报警", not same["separated"] and "污染" in same["detail"],
          same["detail"])

    # ---- 一页纸现状 ----
    text = linkage.summary(memory, {"values": [{"id": "V1", "content": "别追问"}]},
                           user_profile, ai_profile)
    check("联动现状能打成一份摘要",
          "技能联动" in text and "价值观联动" in text and "人格联动" in text, text[:40])
    check("摘要里能看到证据条数", "证据记忆 1 条" in text, text[-80:])

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
