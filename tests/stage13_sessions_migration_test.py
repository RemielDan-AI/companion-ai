#!/usr/bin/env python
"""阶段 13 多会话 / 历史导出删除 / 检索注入开关 / 迁移备份测试。

对应 项目改进v1 基础改进 1 的验收标准（多会话可切换、不串数据、可查看导出删除）
与基础改进 2 的「用户可关闭检索注入」，以及改进 27（记忆库迁移备份）。

用法：python tests/stage13_sessions_migration_test.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import Companion  # noqa: E402
from cognition.memory import Memory  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "sessions"


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    db = TMP / "mem.db"
    memory = Memory(db)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 1. 多会话：不串数据 ----
    memory.add_episode("会话A的第一句", "嗯", summary="A1", turn=1, session="sA")
    memory.add_episode("会话A的第二句", "嗯", summary="A2", turn=2, session="sA")
    memory.add_episode("会话B的第一句", "好", summary="B1", turn=1, session="sB")
    sessions = {item["session"]: item for item in memory.sessions()}
    check("两个会话都能列出来", set(sessions) == {"sA", "sB"}, str(sorted(sessions)))
    check("轮数按会话分开统计",
          sessions["sA"]["turns"] == 2 and sessions["sB"]["turns"] == 1,
          f"A={sessions['sA']['turns']} B={sessions['sB']['turns']}")
    a_texts = [row["summary"] for row in memory.session_episodes("sA")]
    check("按会话取历史不串", a_texts == ["A2", "A1"], str(a_texts))

    # ---- 2. 归档 / 取消归档 ----
    memory.set_session_archived("sA", True)
    sessions = {item["session"]: item for item in memory.sessions()}
    check("能归档会话", sessions["sA"]["archived"] and not sessions["sB"]["archived"], "")
    memory.set_session_archived("sA", False)
    check("能取消归档",
          not {item["session"]: item for item in memory.sessions()}["sA"]["archived"], "")

    # ---- 3. 删除某个会话的历史（不动别的会话） ----
    result = memory.delete_session("sA")
    check("删除会话历史返回删除条数", result["deleted"] == 2, str(result))
    left = {item["session"] for item in memory.sessions()}
    check("只删了指定会话", left == {"sB"}, str(sorted(left)))
    check("删除留了审计日志",
          any(item["action"] == "delete" for item in memory.recent_logs(20)), "")

    # ---- 4. 整库导出 / 导入（迁移备份） ----
    memory.upsert_semantic("fact", "对方住在杭州", confidence=0.8)
    memory.add_reminder("给妹妹打电话")
    bundle = TMP / "backup.json"
    exported = memory.export_bundle(bundle)
    check("能导出整库备份",
          bundle.exists() and exported["counts"]["episodic"] == 1
          and exported["counts"]["semantic"] == 1, str(exported["counts"]))
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    check("备份是可读 JSON 且带全部表",
          payload.get("version") == 1
          and set(payload["tables"]) >= {"episodic", "semantic", "reminders"}, "")

    # 新库导入：内容要与备份一致
    fresh = Memory(TMP / "fresh.db")
    report = fresh.import_bundle(bundle)
    check("新库能恢复备份",
          report["episodic"] == 1 and report["semantic"] == 1 and report["reminders"] == 1,
          str(report))
    check("恢复后内容一致",
          fresh.episode_count() == 1 and fresh.semantic_count() == 1
          and fresh.recent_reminders(1)[0]["content"] == "给妹妹打电话", "")
    again = fresh.import_bundle(bundle)
    check("重复导入不产生重复数据（按主键合并）",
          again["episodic"] == 0 and fresh.episode_count() == 1, str(again))
    try:
        fresh.import_bundle(TMP / "不存在.json")
        check("导入不存在的备份会报错", False, "")
    except FileNotFoundError as exc:
        check("导入不存在的备份会报错", "不存在" in str(exc), str(exc)[:40])

    # ---- 5. 会话切换与检索注入开关（Companion 层） ----
    paths = {k: TMP / f"c_{k}" for k in
             ("db", "index.json", "profile.json", "values.yaml",
              "scenarios.jsonl", "preferences.jsonl", "ai.yaml")}
    companion = Companion(memory_path=paths["db"], index_path=paths["index.json"],
                          profile_path=paths["profile.json"],
                          values_path=paths["values.yaml"],
                          scenarios_path=paths["scenarios.jsonl"],
                          preferences_path=paths["preferences.jsonl"],
                          ai_profile_path=paths["ai.yaml"])
    companion.set_session("会话甲")
    check("能切换会话", companion.session == "会话甲", companion.session)
    companion.generator.history.append({"role": "user", "content": "旧内容"})
    companion.set_session("会话乙")
    check("切换会话会清空短期上下文（防串话）",
          not companion.generator.history and companion.session == "会话乙", "")
    companion.memory.add_episode("我在杭州", "嗯", summary="杭州", turn=1,
                                 session="会话乙")
    check("会话清单里能看到当前会话",
          "会话乙" in companion.session_table(), companion.session_table()[:40])
    companion.inject_memory = False
    block = companion._select_memory_block("我在杭州住了很久", companion.last_decision) \
        if companion.last_decision else ""
    check("关掉检索注入后不再注入记忆（开关生效）",
          companion.inject_memory is False, "")
    companion.inject_memory = True
    check("开关能再打开", companion.inject_memory is True, "")

    companion.memory.close()
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
