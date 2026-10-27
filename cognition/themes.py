"""记忆抽象与聚类（项目改进v1 改进 19）。

做的事：把一堆具体记忆聚成主题，从中提炼一句「模式」，作为**高层记忆**存起来，
每条主题都记着成员编号，所以随时能追溯回原始记忆、也不会因为抽象而丢细节。

聚类用二元组 Jaccard 相似度做贪心合并（和去重合并同一套度量）；
提炼模式优先交给模型（给它几条成员，让它说一句共同点），模型不可用时退化成模板句，
保证离线也能用、也保证测试可复现。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

from cognition.memory import Memory

DDL = """
CREATE TABLE IF NOT EXISTS memory_theme (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    members    TEXT DEFAULT '[]',
    count      INTEGER DEFAULT 0,
    source     TEXT DEFAULT 'cluster',
    UNIQUE(title)
);
"""

PROMPT = ("下面几条是同一个人的零碎记忆。用一句中文说出发共同点（这是他的习惯或状态），"
          "只输出这一句，不要解释，不要罗列：\n\n{items}")


def _ensure(memory: Memory) -> None:
    memory.conn.executescript(DDL)
    memory.conn.commit()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def collect(memory: Memory, limit: int = 400) -> list[dict]:
    """取出可参与聚类的条目（情景摘要 + 语义内容）。"""
    items: list[dict] = []
    for row in memory.recent_episodes(limit=limit):
        text = str(row.get("summary") or "").strip()
        if text:
            items.append({"key": f"episodic#{row['id']}", "text": text,
                          "emotion": row.get("emotion") or ""})
    for row in memory.all_semantic(status="active"):
        text = str(row.get("content") or "").strip()
        if text:
            items.append({"key": f"semantic#{row['id']}", "text": text,
                          "emotion": row.get("emotion") or ""})
    return items


def cluster(memory: Memory, threshold: float = 0.22, min_size: int = 3) -> list[list[dict]]:
    """贪心聚类：相似度够高就并进同一个组，最后只留够大的组。

    阈值默认 0.22（二元组 Jaccard）——真实日记里同一主题反复出现时措辞往往相近，
    0.22 能把它们并起来，又不至于把不相关的事混成一堆；嫌松/紧可以调这个参数。
    """
    items = collect(memory)
    groups: list[list[dict]] = []
    for item in items:
        placed = False
        for group in groups:
            score = sum(Memory.similarity(item["text"], member["text"])
                        for member in group) / len(group)
            if score >= threshold:
                group.append(item)
                placed = True
                break
        if not placed:
            groups.append([item])
    return [group for group in groups if len(group) >= min_size]


def _title(group: list[dict]) -> str:
    """标题取组内最长的共同子串（找不到就用最短那条的前 8 字）。"""
    texts = [item["text"] for item in group]
    shortest = min(texts, key=len)
    best = ""
    for start in range(len(shortest)):
        for end in range(len(shortest), start + 1, -1):
            piece = shortest[start:end]
            if len(piece) <= len(best):
                break
            if all(piece in text for text in texts):
                best = piece
    if best:
        return best[:24]
    return shortest[:8]


def _pattern(group: list[dict], generator=None) -> tuple[str, str]:
    """提炼一句模式：优先让模型说，失败就用模板句（保证离线可用）。"""
    lines = [f"- {item['text']}" for item in group[:6]]
    if generator is not None:
        try:
            text = generator.complete("你是记忆整理助手，只输出一句话。",
                                      PROMPT.format(items="\n".join(lines)),
                                      max_tokens=80, temperature=0.3)
            text = str(text or "").strip().strip('"').strip("「」").strip()
            if text:
                return text[:120], "model"
        except Exception:
            pass
    return (f"反复出现的主题（{len(group)} 条）：" + "；".join(
        item["text"][:12] for item in group[:3])), "template"


def build(memory: Memory, generator=None, threshold: float = 0.22,
          min_size: int = 3) -> list[dict]:
    """重建主题：聚类 → 提炼 → 落库（同标题覆盖更新，成员合并）。"""
    _ensure(memory)
    groups = cluster(memory, threshold, min_size)
    saved: list[dict] = []
    for group in groups:
        title = _title(group)
        if not title.strip():
            continue
        content, source = _pattern(group, generator)
        members = sorted({item["key"] for item in group})
        existing = memory.conn.execute(
            "SELECT id, members, count FROM memory_theme WHERE title=?",
            (title,)).fetchone()
        if existing:
            merged = sorted(set(json.loads(existing["members"] or "[]")) | set(members))
            memory.conn.execute(
                "UPDATE memory_theme SET content=?, members=?, count=?, updated_at=?,"
                " source=? WHERE id=?",
                (content, json.dumps(merged, ensure_ascii=False), len(merged), _now(),
                 source, existing["id"]))
            theme_id = int(existing["id"])
        else:
            cur = memory.conn.execute(
                "INSERT INTO memory_theme (created_at, updated_at, title, content,"
                " members, count, source) VALUES (?,?,?,?,?,?,?)",
                (_now(), _now(), title, content,
                 json.dumps(members, ensure_ascii=False), len(members), source))
            theme_id = int(cur.lastrowid)
        memory.conn.commit()
        memory.log("abstract", "theme", theme_id, f"主题「{title}」{len(members)} 条（{source}）")
        saved.append({"id": theme_id, "title": title, "content": content,
                      "count": len(members), "source": source})
    return saved


def themes(memory: Memory, limit: int = 50) -> list[dict]:
    """列出已存的高层记忆（主题），按成员数从多到少。"""
    _ensure(memory)
    rows = memory.conn.execute(
        "SELECT * FROM memory_theme ORDER BY count DESC, id DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["member_keys"] = json.loads(item.get("members") or "[]")
        out.append(item)
    return out


def trace(memory: Memory, theme_id: int, limit: int = 20) -> list[dict]:
    """追溯：把主题的成员原文取回来（抽象结果可追溯到原始记忆）。"""
    _ensure(memory)
    row = memory.conn.execute("SELECT * FROM memory_theme WHERE id=?",
                              (int(theme_id),)).fetchone()
    if not row:
        return []
    out = []
    for key in json.loads(row["members"] or "[]")[:limit]:
        try:
            entry = memory.entry_by_key(key)
        except Exception:
            entry = None
        if not entry:
            continue
        out.append({"key": key, "text": entry.get("summary") or entry.get("content") or "",
                    "created_at": entry.get("created_at") or ""})
    return out


def drop(memory: Memory, theme_id: int) -> bool:
    _ensure(memory)
    cur = memory.conn.execute("DELETE FROM memory_theme WHERE id=?", (int(theme_id),))
    memory.conn.commit()
    if cur.rowcount:
        memory.log("drop", "theme", int(theme_id), "")
        return True
    return False


def matches(memory: Memory, query: str, limit: int = 2,
            threshold: float = 0.3) -> list[dict]:
    """检索时用：找出与当前输入相关的主题（当作高层记忆注入）。"""
    _ensure(memory)
    from engine.retrieve import keywords, lexical_score
    grams = keywords(query or "")
    if not grams:
        return []
    scored = []
    for item in themes(memory, limit=100):
        score = max(lexical_score(grams, item["title"]),
                    lexical_score(grams, item["content"]) * 0.8)
        if score >= threshold:
            scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    return [dict(item, score=round(score, 3)) for score, item in scored[:limit]]
