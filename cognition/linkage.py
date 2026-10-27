"""与技能/价值观系统的联动（项目改进v1 模块一 改进 29-31）。

三件事：
- **改进 30（与技能联动）**：每次技能/工具调用单独记一条 skill_log（日志与记忆**分离**），
  同时把这轮的情景记忆与那条技能日志**关联**起来，所以"当时为什么查天气"能倒查；
  记忆也能触发技能——待办（reminders）就是记忆触发技能的现成例子。
- **改进 29（与价值观联动）**：价值观条目（V1、V2…）与促成它的情景记忆建关联，
  形成证据链；value_evidence() 能把某个价值观的全部证据记忆取回来。
- **改进 31（与人格联动）**：AI 人格档案本来就独立存 config/ai_profile.yaml，
  与用户记忆互不污染；persona_separation() 负责把这件事检查一遍，避免哪天被人写混。

编号约定：value#V1、skill#12、episodic#3、semantic#5 —— 统一走 memory_link，
所以 /links 一处就能看全。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

from cognition.memory import Memory

DDL = """
CREATE TABLE IF NOT EXISTS skill_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    skill_id   TEXT DEFAULT '',
    tool       TEXT DEFAULT '',
    detail     TEXT DEFAULT '',
    result     TEXT DEFAULT '',
    ok         INTEGER DEFAULT 1,
    episode_id INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_skill_log_created ON skill_log(created_at);
"""


def _ensure(memory: Memory) -> None:
    memory.conn.executescript(DDL)
    memory.conn.commit()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def log_skill(memory: Memory, skill_id: str, tool: str = "", detail: str = "",
              result: str = "", ok: bool = True, episode_id: int = 0) -> int:
    """记一条技能调用（与记忆分离），并关联到这一轮情景记忆。"""
    _ensure(memory)
    cur = memory.conn.execute(
        "INSERT INTO skill_log (created_at, skill_id, tool, detail, result, ok,"
        " episode_id) VALUES (?,?,?,?,?,?,?)",
        (_now(), skill_id, tool, detail[:200], result[:300], 1 if ok else 0,
         int(episode_id or 0)))
    memory.conn.commit()
    skill_id_num = int(cur.lastrowid)
    if episode_id:
        try:
            memory.add_link(f"skill#{skill_id_num}", f"episodic#{int(episode_id)}",
                            "skill_use", 0.6, f"{skill_id} 用在这轮对话里")
        except Exception:
            pass
    return skill_id_num


def skill_logs(memory: Memory, limit: int = 20) -> list[dict]:
    """技能调用日志（改进 30：技能日志与记忆分离但可关联）。"""
    _ensure(memory)
    rows = memory.conn.execute(
        "SELECT * FROM skill_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def skill_episode(memory: Memory, skill_log_id: int) -> dict | None:
    """从技能日志倒查当时那一轮对话。"""
    _ensure(memory)
    row = memory.conn.execute("SELECT * FROM skill_log WHERE id=?",
                              (int(skill_log_id),)).fetchone()
    if not row or not row["episode_id"]:
        return None
    return memory.entry("episodic", int(row["episode_id"]))


def link_value_evidence(memory: Memory, value_ids, episode_id: int,
                        note: str = "") -> int:
    """把价值观条目与促成它的情景记忆关联（证据链）。"""
    if not episode_id:
        return 0
    if isinstance(value_ids, str):
        value_ids = [value_ids]
    made = 0
    for value_id in value_ids or []:
        try:
            memory.add_link(f"value#{value_id}", f"episodic#{int(episode_id)}",
                            "evidence", 0.7, note or "价值观证据")
            made += 1
        except Exception:
            continue
    return made


def value_evidence(memory: Memory, value_id: str, limit: int = 20) -> list[dict]:
    """某个价值观的全部证据记忆（改进 29：变化可回溯）。"""
    out: list[dict] = []
    for link in memory.links(f"value#{value_id}", kind="evidence", limit=limit):
        other = link["target"] if link["source"] == f"value#{value_id}" else link["source"]
        try:
            entry = memory.entry_by_key(other)
        except Exception:
            entry = None
        if entry:
            out.append({"key": other, "text": entry.get("summary") or "",
                        "created_at": entry.get("created_at") or "",
                        "note": link.get("note") or ""})
    return out


def pending_reminders(memory: Memory) -> list[dict]:
    """记忆触发技能：待办（提醒）是现成的例子——记忆里存着，轮到时用得上。"""
    return memory.recent_reminders(limit=20, status="open")


def persona_separation(profile_path=None, ai_profile_path=None) -> dict:
    """检查 AI 人格档案与用户画像确实分开了（改进 31）。"""
    import pathlib as _pathlib

    from cognition import ai_profile

    user_path = _pathlib.Path(profile_path) if profile_path else \
        _pathlib.Path(__file__).resolve().parent / "profile.json"
    ai_path = _pathlib.Path(ai_profile_path) if ai_profile_path else ai_profile.PROFILE_PATH
    ok = user_path.resolve() != ai_path.resolve()
    detail = "两份档案在不同文件里"
    if not ok:
        detail = "⚠ 用户画像与 AI 档案指向同一个文件，会互相污染"
    return {"separated": ok, "user_profile": str(user_path),
            "ai_profile": str(ai_path), "detail": detail}


def summary(memory: Memory, value_store: dict | None = None,
            profile_path=None, ai_profile_path=None) -> str:
    """联动现状一页纸：技能调用、价值观证据、人格分离。"""
    logs = skill_logs(memory, limit=5)
    lines = ["【技能联动】"]
    if logs:
        for item in logs:
            lines.append(f"  #{item['id']} {item['skill_id']}·{item['tool']}"
                         f"　{'成功' if item['ok'] else '失败'}"
                         f"　{item['result'][:24]}"
                         + (f"　→ 对话 episodic#{item['episode_id']}"
                            if item["episode_id"] else ""))
    else:
        lines.append("  （还没有技能调用记录）")
    todos = pending_reminders(memory)
    lines.append(f"  记忆触发的待办：{len(todos)} 条"
                 + ("（" + "、".join(str(t["content"])[:12] for t in todos[:3]) + "）"
                    if todos else ""))
    lines.append("【价值观联动】")
    values = ((value_store or {}).get("values") or [])
    if not values:
        lines.append("  （价值观库还是空的）")
    for item in values[:5]:
        evidence = value_evidence(memory, str(item.get("id", "")), limit=5)
        lines.append(f"  {item.get('id')}　{str(item.get('content'))[:24]}"
                     f"　证据记忆 {len(evidence)} 条")
    lines.append("【人格联动】")
    lines.append(f"  {persona_separation(profile_path, ai_profile_path)['detail']}")
    return "\n".join(lines)
