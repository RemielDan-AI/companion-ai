"""反馈收集：点赞 / 点踩 / 纠正，落到 memory.db 的 feedback 表。

阶段 2 新增。反馈在阶段 5 会被当成价值观信号使用，所以这里如实记录原始信息，
不做任何解释性加工。
"""
from __future__ import annotations

from cognition.memory import Memory

RATINGS = {"up": "点赞", "down": "点踩", "correction": "纠正"}


def record(memory: Memory, rating: str, note: str = "",
           episodic_id: int | None = None) -> int:
    if rating not in RATINGS:
        raise ValueError(f"未知反馈类型：{rating}（可用：{'/'.join(RATINGS)}）")
    return memory.add_feedback(rating, note=note, episodic_id=episodic_id)


def last_episode_id(memory: Memory) -> int | None:
    episodes = memory.recent_episodes(1)
    return episodes[0]["id"] if episodes else None


def record_last(memory: Memory, rating: str, note: str = "") -> int:
    """对最近一轮的反馈（交互里最常用）。"""
    return record(memory, rating, note=note, episodic_id=last_episode_id(memory))


def summarize(memory: Memory) -> str:
    counts = memory.feedback_stats()
    if not counts:
        return "还没有反馈记录"
    parts = [f"{RATINGS.get(key, key)} {value} 次" for key, value in counts.items()]
    return "、".join(parts)
