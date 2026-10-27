"""记忆重要度生命周期：初始赋值、衰减、回升、冷存储判定。

对应 项目改进v1.txt 的基础改进 3 与补充优化项 12/13/14/15/16：
- 每条记忆带重要度与时间戳，重要度随时间衰减，有下限不归零；
- 不同类型不同半衰期（事实慢、情绪中、琐事快、价值观证据几乎不衰减）；
- 被检索/被提起 → 回升，但有上限（不超过初始最高值）；
- 长期低重要度 → 冷存储，检索仍可命中，命中后自动唤醒；
- 用户可标记「永久重要」「可遗忘」「暂时保留」。

本模块只做纯函数计算，不碰数据库；落库由 cognition/memory.py 负责。
"""
from __future__ import annotations

import datetime as dt
import pathlib

import yaml

DECAY_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "decay.yaml"

DEFAULTS: dict = {
    "enabled": True,
    "floor": 0.05,               # 重要度下限：不归零
    "max_importance": 5.0,       # 重要度上限（回升不超过它）
    "archive_threshold": 0.15,   # 低于此值视为冷
    "archive_idle_days": 30,     # 且这么久没被访问 → 转冷存储
    "boost": {
        "retrieved": 0.25,       # 被检索命中
        "mentioned": 0.40,       # 被用户主动提起
        "related": 0.15,         # 与当前话题相关（临时，不写回）
    },
    "half_life_days": {
        "fact": 180,             # 事实：慢
        "preference": 120,       # 偏好：偏慢
        "relation": 365,         # 关系：很慢
        "event": 60,             # 事件：中
        "emotion": 45,           # 情绪：中
        "trivia": 14,            # 琐事：快
        "value": 3650,           # 价值观证据：几乎不衰减
        "default": 60,
    },
    "retention_factor": {        # 用户标记对半衰期的影响
        "normal": 1.0,
        "temporary": 2.0,        # 暂时保留：延长
        "forgettable": 0.25,     # 可遗忘：加速
        "permanent": 0.0,        # 永久重要：不衰减（特殊处理）
    },
    "initial": {                 # 新记忆的初始重要度（1–5）
        "explicit": 4.5,         # 显式说出的信息
        "strong_emotion": 4.5,   # 情绪强烈
        "repeated": 4.0,         # 重复出现
        "chitchat": 2.0,         # 普通闲聊
        "default": 3.0,
    },
    "rank_weights": {            # 检索排序：相关性 + 重要度 + 新鲜度
        "relevance": 0.60,
        "importance": 0.25,
        "recency": 0.15,
    },
    "max_inject_chars": 380,     # 主动注入的记忆块上限（改进 10 的上下文预算）
    "temporary_boost_ttl": 1,    # 临时提升只影响本轮（回合数）
}


def load_rules(path: pathlib.Path | str = DECAY_PATH) -> dict:
    """读取衰减配置；缺项用默认值补齐（配置改坏也不会崩）。"""
    rules = {}
    path = pathlib.Path(path)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rules = yaml.safe_load(fh) or {}
        except Exception:
            rules = {}
    merged = dict(DEFAULTS)
    for key, value in (rules or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


# ---------------- 时间 ----------------
def parse_time(text: str) -> dt.datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def days_since(text: str, at: dt.datetime | None = None) -> float:
    stamp = parse_time(text)
    if stamp is None:
        return 0.0
    at = at or dt.datetime.now()
    return max(0.0, (at - stamp).total_seconds() / 86400.0)


# ---------------- 类型与半衰期 ----------------
KIND_ALIASES = {
    "fact": "fact", "facts": "fact", "事实": "fact",
    "preference": "preference", "偏好": "preference",
    "relation": "relation", "关系": "relation",
    "event": "event", "事件": "event",
    "emotion": "emotion", "情绪": "emotion",
    "trivia": "trivia", "琐事": "trivia",
    "value": "value", "values": "value", "价值观": "value",
}


def kind_of(entry: dict) -> str:
    """推断这条记忆该用哪条衰减曲线。"""
    raw = str(entry.get("kind") or "").strip().lower()
    if raw in KIND_ALIASES:
        return KIND_ALIASES[raw]
    if entry.get("emotion"):
        return "emotion"
    if str(entry.get("source") or "") == "value":
        return "value"
    return "event"


def half_life_days(entry: dict, rules: dict | None = None) -> float:
    rules = rules or DEFAULTS
    retention = str(entry.get("retention") or "normal")
    factor = float((rules.get("retention_factor") or {}).get(retention, 1.0))
    if retention == "permanent" or factor <= 0:
        return 0.0                       # 0 表示不衰减
    kind = kind_of(entry)
    base = float((rules.get("half_life_days") or {}).get(
        kind, (rules.get("half_life_days") or {}).get("default", 60)))
    return max(0.1, base * factor)


# ---------------- 衰减与回升 ----------------
def anchor_time(entry: dict) -> str:
    """衰减基准时间：最后一次被强化（访问）的时间，没有就用创建时间。"""
    return str(entry.get("last_access_at") or entry.get("updated_at")
               or entry.get("created_at") or "")


def decayed_importance(entry: dict, rules: dict | None = None,
                       at: dt.datetime | None = None) -> float:
    """当前有效重要度。base 存在 importance 字段里，随时间指数衰减。"""
    rules = rules or DEFAULTS
    base = float(entry.get("importance") or (rules.get("initial") or {}).get("default", 3))
    floor = float(rules.get("floor", 0.05))
    if not rules.get("enabled", True):
        return base
    half = half_life_days(entry, rules)
    if half <= 0:
        return base                       # 永久重要：不衰减
    elapsed = days_since(anchor_time(entry), at)
    value = base * (0.5 ** (elapsed / half))
    return max(floor, round(value, 4))


def peak_importance(entry: dict, rules: dict | None = None) -> float:
    """这条记忆的回升上限：初始（或升过权后的）最高值，不是全局上限。"""
    rules = rules or DEFAULTS
    peak = float(entry.get("peak") or 0) or float(entry.get("importance") or 0)
    if peak <= 0:
        peak = float(rules.get("max_importance", 5.0))
    return min(float(rules.get("max_importance", 5.0)), peak)


def boost_importance(entry: dict, amount: float, rules: dict | None = None,
                     current: float | None = None) -> float:
    """被检索/被提起后的新 base 重要度。

    在「当前有效值」上加分，再封顶到这条记忆的峰值——所以久远记忆能被逐步唤醒，
    但不会因为反复访问就无限膨胀（改进 14：回升有上限）。
    """
    rules = rules or DEFAULTS
    base = decayed_importance(entry, rules) if current is None else float(current)
    return min(peak_importance(entry, rules), round(base + float(amount), 4))


def clamp_self_mention(entry: dict, current: float, rules: dict | None = None) -> float:
    """AI **自己**复述某条记忆时的重要度上限（借鉴 Cyrene-Agent 的 decay − ε）。

    为什么要这条：如果「自己提到」也给净加分，一条记忆只要被反复复述就能一直涨，
    形成自激循环——这正是我们实测到的"复读/口癖"的机制性来源。
    所以把它钳制在「当前有效值 − ε」以下：**被自己复述只会继续缓慢衰减，永不净涨**。
    """
    rules = rules or DEFAULTS
    eps = float(rules.get("self_mention_epsilon", 0.01))
    ceiling = max(0.0, float(current) - eps)
    boosted = boost_importance(entry, 0.0, rules, current=current)
    return round(min(boosted, ceiling), 4)


def virtual_importance(entry: dict, rules: dict | None = None) -> float:
    """临时提升：只影响本轮排序，不写回数据库。"""
    rules = rules or DEFAULTS
    bump = float((rules.get("boost") or {}).get("related", 0.0))
    return min(float(rules.get("max_importance", 5.0)),
               decayed_importance(entry, rules) + bump)


def should_archive(entry: dict, rules: dict | None = None,
                   at: dt.datetime | None = None) -> bool:
    """是否该转入冷存储：够冷 + 够久没被碰过 + 用户没标永久重要。"""
    rules = rules or DEFAULTS
    if int(entry.get("archived") or 0) == 1:
        return False
    if str(entry.get("retention") or "") == "permanent":
        return False
    cold = decayed_importance(entry, rules, at) < float(rules.get("archive_threshold", 0.15))
    idle = days_since(anchor_time(entry), at) >= float(rules.get("archive_idle_days", 30))
    return bool(cold and idle)


def initial_importance(text: str, emotion_info: dict | None = None,
                       repeated: bool = False, explicit: bool = False,
                       rules: dict | None = None) -> float:
    """给新记忆定初始重要度：显式信息高、情绪强烈高、重复出现高、闲聊低。"""
    rules = rules or DEFAULTS
    table = rules.get("initial") or {}
    text = (text or "").strip()
    if explicit:
        return float(table.get("explicit", 4.5))
    intensity = 0
    if emotion_info:
        try:
            intensity = int(emotion_info.get("intensity") or 0)
        except Exception:
            intensity = 0
    if intensity >= 4:
        return float(table.get("strong_emotion", 4.5))
    if repeated:
        return float(table.get("repeated", 4.0))
    if len(text) <= 12 or str((emotion_info or {}).get("emotion") or "平静") == "平静":
        return float(table.get("chitchat", 2.0))
    return float(table.get("default", 3.0))


# ---------------- 排序 ----------------
def recency_score(entry: dict, rules: dict | None = None) -> float:
    """新鲜度 0–1：越近越高（按半衰期归一，避免所有旧记忆都贴底）。"""
    rules = rules or DEFAULTS
    half = half_life_days(entry, rules) or float(
        (rules.get("half_life_days") or {}).get("default", 60))
    return round(0.5 ** (days_since(anchor_time(entry)) / max(1.0, half)), 4)


def rank_score(entry: dict, relevance: float, rules: dict | None = None) -> float:
    """综合排序：相关性 + 重要度 + 新鲜度（权重可配）。"""
    rules = rules or DEFAULTS
    weights = rules.get("rank_weights") or {}
    max_imp = float(rules.get("max_importance", 5.0)) or 5.0
    importance = virtual_importance(entry, rules) / max_imp
    return round(
        float(weights.get("relevance", 0.6)) * float(relevance)
        + float(weights.get("importance", 0.25)) * importance
        + float(weights.get("recency", 0.15)) * recency_score(entry, rules),
        4,
    )
