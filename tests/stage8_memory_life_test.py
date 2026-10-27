#!/usr/bin/env python
"""阶段 8 记忆生命周期测试（项目改进v1.txt：基础改进 3 + 优化项 12/13/14/15/16/22/7/10）。

全部用独立临时库，不碰真实记忆。
用法：python tests/stage8_memory_life_test.py
"""
from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition import decay as decay_engine  # noqa: E402
from cognition.memory import Memory  # noqa: E402
from cognition.vectors import VectorIndex  # noqa: E402
from engine.retrieve import build_memory_block, retrieve  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp"
RULES = decay_engine.load_rules()


def days_ago(n: float) -> str:
    return (dt.datetime.now() - dt.timedelta(days=n)).isoformat(timespec="seconds")


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    db = TMP / "life.db"
    index_file = TMP / "life_index.json"
    for path in (db, index_file):
        if path.exists():
            path.unlink()
    memory = Memory(db)
    index = VectorIndex(index_file)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 1. 初始重要度：显式信息高、闲聊低（改进 13） ----
    explicit = decay_engine.initial_importance("我叫林一，住在杭州。", None, explicit=True)
    strong = decay_engine.initial_importance("我今天特别难受",
                                             {"emotion": "难过", "intensity": 5})
    chitchat = decay_engine.initial_importance("嗯嗯", {"emotion": "平静", "intensity": 1})
    check("显式信息初始重要度高", explicit >= 4.0, f"{explicit}")
    check("情绪强烈初始重要度高", strong >= 4.0, f"{strong}")
    check("普通闲聊初始重要度低", chitchat <= 2.5, f"{chitchat}")

    # ---- 2. 衰减：不同类型不同速度，有下限不归零（改进 3/12） ----
    base = {"importance": 4.0, "created_at": days_ago(0), "last_access_at": "",
            "retention": "normal"}
    at = dt.datetime.now() + dt.timedelta(days=30)
    fact = decay_engine.decayed_importance(dict(base, kind="fact"), RULES, at)
    event = decay_engine.decayed_importance(dict(base, kind="event"), RULES, at)
    trivia = decay_engine.decayed_importance(dict(base, kind="trivia"), RULES, at)
    value = decay_engine.decayed_importance(dict(base, kind="value"), RULES, at)
    check("事实比事件衰减慢", fact > event, f"fact={fact} event={event}")
    check("事件比琐事衰减慢", event > trivia, f"event={event} trivia={trivia}")
    check("价值观证据几乎不衰减", value > 3.9, f"{value}")
    floor = decay_engine.decayed_importance(dict(base, kind="trivia"), RULES,
                                            dt.datetime.now() + dt.timedelta(days=3650))
    check("衰减有下限、不归零", floor >= RULES["floor"], f"{floor}")

    # ---- 3. 用户标记：永久重要 / 可遗忘 / 暂时保留（改进 16） ----
    perm = decay_engine.decayed_importance(dict(base, retention="permanent"), RULES, at)
    forget = decay_engine.decayed_importance(dict(base, retention="forgettable"), RULES, at)
    temp = decay_engine.decayed_importance(dict(base, retention="temporary"), RULES, at)
    check("永久重要不衰减", perm == 4.0, f"{perm}")
    check("可遗忘衰减更快", forget < event, f"{forget}")
    check("暂时保留衰减更慢", temp > event, f"{temp}")

    # ---- 4. 回升：有上限、不超过峰值（改进 14） ----
    entry = {"importance": 3.0, "peak": 4.0, "created_at": days_ago(60),
             "last_access_at": "", "retention": "normal", "kind": "event"}
    dec = decay_engine.decayed_importance(entry, RULES)
    up1 = decay_engine.boost_importance(entry, RULES["boost"]["retrieved"], RULES)
    up5 = decay_engine.boost_importance(entry, 5.0, RULES)
    check("检索命中会回升", up1 > dec, f"{dec} → {up1}")
    check("回升封顶在峰值", up5 == 4.0, f"{up5}")

    # ---- 5. 落库：写入 → 访问 → 回升 → 冷存储 → 唤醒（改进 3/15/22） ----
    eid = memory.add_episode("我养了只猫叫团子", "团子真可爱", summary="聊到猫",
                             emotion="开心", importance=4, turn=1)
    row = memory.entry("episodic", eid)
    check("新记忆带峰值与访问字段",
           float(row["peak"]) == 4.0 and int(row["access_count"]) == 0, str(dict(row))[:0] or
           f"peak={row['peak']} count={row['access_count']}")

    memory.touch("episodic", eid, "retrieved")
    row2 = memory.entry("episodic", eid)
    check("访问后次数与时间被记录",
           int(row2["access_count"]) == 1 and bool(row2["last_access_at"]), "")

    # 造一条很旧、重要度很低的记忆：手动把时间与重要度改旧
    old_id = memory.add_episode("随便聊了句天气", "嗯", summary="闲聊", importance=1, turn=2)
    memory.update_entry("episodic", old_id, summary="闲聊", importance=1)
    # 先验证"还不够冷"不会被归档（90 天、事件半衰期 60 天，有效重要度约 0.35）
    memory.conn.execute(
        "UPDATE episodic SET created_at=?, last_access_at='', peak=1.0, importance=1.0"
        " WHERE id=?", (days_ago(90), old_id))
    memory.conn.commit()
    not_yet = [m["id"] for m in memory.archive_cold(RULES)]
    check("还不够冷的记忆不会被归档", old_id not in not_yet, f"{not_yet}")

    # 再放到足够久（400 天），就该进冷存储了
    memory.conn.execute("UPDATE episodic SET created_at=? WHERE id=?",
                        (days_ago(400), old_id))
    memory.conn.commit()
    moved = memory.archive_cold(RULES)
    archived_ids = [m["id"] for m in moved]
    check("长期低重要度记忆转入冷存储", old_id in archived_ids, f"{archived_ids}")
    check("刚访问过的记忆不会被归档", eid not in archived_ids, "")

    # 冷存储记忆：主动注入不取，按需检索仍能命中并唤醒
    active = memory.recent_episodes(limit=200)
    active_ids = [r["id"] for r in active if not int(r["archived"] or 0)]
    check("冷存储不占主动注入名额", old_id not in active_ids, f"{active_ids}")

    result = retrieve("天气", memory, index, include_cold=False)
    check("默认检索跳过冷存储",
           all(item["id"] != old_id for item in result["episodes"]),
           f"{[i['id'] for i in result['episodes']]}")
    result2 = retrieve("天气", memory, index, include_cold=True)
    hit = [i for i in result2["episodes"] if i["id"] == old_id]
    check("按需检索能命中冷存储", bool(hit), f"{[i['id'] for i in result2['episodes']]}")
    woken = memory.entry("episodic", old_id)
    check("命中后自动唤醒", int(woken["archived"]) == 0 and int(woken["access_count"]) >= 1,
           f"archived={woken['archived']} count={woken['access_count']}")

    # ---- 6. 用户手动控制（改进 16） ----
    memory.set_retention("episodic", eid, "permanent")
    perm_row = memory.entry("episodic", eid)
    check("可标记永久重要", perm_row["retention"] == "permanent", "")
    check("永久重要不再被归档",
           not decay_engine.should_archive(dict(perm_row, created_at=days_ago(999)), RULES), "")
    memory.set_retention("episodic", eid, "forgettable")
    check("可标记可遗忘", memory.entry("episodic", eid)["retention"] == "forgettable", "")

    # ---- 7. 审计日志（改进 22） ----
    logs = memory.recent_logs(limit=50)
    actions = {item["action"] for item in logs}
    check("写入了审计日志", memory.log_count() >= 4, f"{memory.log_count()} 条")
    check("日志里有检索/归档/唤醒等动作",
           {"retrieved", "archive", "wake"} <= actions, f"{sorted(actions)}")
    check("写入也会记日志（走提取链路时）",
           memory.log("write", "episodic", 999, "测试") > 0
           and any(i["action"] == "write" for i in memory.recent_logs(limit=5)), "")

    # ---- 8. 用户可编辑：改与删（改进 23） ----
    memory.update_entry("episodic", eid, summary="改成：团子是橘猫")
    check("可手动修改记忆",
           memory.entry("episodic", eid)["summary"] == "改成：团子是橘猫", "")
    memory.delete_entry("episodic", eid)
    check("可删除记忆", memory.entry("episodic", eid) is None, "")
    check("删除也留了日志",
           any(item["action"] == "delete" for item in memory.recent_logs(limit=20)), "")

    # ---- 9. 检索排序：重要度参与（改进 7） ----
    high = {"id": 1, "importance": 5, "peak": 5, "kind": "fact",
            "created_at": days_ago(1), "last_access_at": "", "retention": "normal"}
    low = {"id": 2, "importance": 1, "peak": 1, "kind": "trivia",
           "created_at": days_ago(60), "last_access_at": "", "retention": "normal"}
    s_high = decay_engine.rank_score(high, 0.5, RULES)
    s_low = decay_engine.rank_score(low, 0.5, RULES)
    check("同等相关性下重要度高的排前面", s_high > s_low, f"{s_high} vs {s_low}")

    # ---- 10. 上下文预算（改进 10） ----
    fake = {"semantic": [{"kind": "fact", "content": "事实" + "长" * 300}], "episodes": []}
    block = build_memory_block(fake)
    limit = int(RULES["max_inject_chars"])
    check("注入块受长度预算约束", len(block) <= limit + 2, f"{len(block)} ≤ {limit}")
    check("预算可从配置改", build_memory_block(fake, max_chars=120).__len__() <= 122, "")

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
