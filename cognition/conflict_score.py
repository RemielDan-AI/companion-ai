"""冲突打分与优先级（借鉴 Cyrene-Agent 的 memory-conflict-score）。

要解决的问题：现在**任何**疑似冲突都会挂起、在用户面前排队等拍板，
但实战里大量"冲突"是噪音——纯词法误判、目标已归档、压根没有证据。
打分之后，只有真正值得问的（high/normal）才冒泡给用户。

权重与阈值照搬原设计（原文逐字），便于日后对照：
    +20 用户明确纠正意图（"你记错了""不是这样"）
    +25/18/10 冲突目标与当前话题的相关度（rag_score ≥.75/.45/其它）
    +20 冲突目标刚被注入过 prompt
    +15/8 证据（both 双方都有证据 / one_side 只有一方）
    +10 本地词法也判定矛盾
    +10/6/3 影响面（high/medium/low）
    -25 目标已归档
    -20 完全没有证据
    -25 这一对刚解决过（别反复问）

    优先级：≥75 high ／ ≥55 normal ／ ≥35 idle ／ 其余 none
    兜底：目标已归档 或 证据为 none → 一律 none（永不打扰用户）
"""
from __future__ import annotations

CORRECTION_WORDS = ("不是这样", "你记错了", "记错了", "我现在不这样",
                    "现在不这样", "不对吧", "你搞错了")

EVIDENCE_POINTS = {"both": 15, "one_side": 8, "none": 0}
IMPACT_POINTS = {"high": 10, "medium": 6, "low": 3}


def rag_points(score: float | None) -> int:
    if score is None:
        return 0
    if score >= 0.75:
        return 25
    if score >= 0.45:
        return 18
    return 10


def has_correction_intent(text: str) -> bool:
    """用户这句话是不是在明确纠正我。"""
    return any(word in str(text or "") for word in CORRECTION_WORDS)


def score_conflict(*, correction_intent: bool = False, rag_score: float | None = None,
                   recent_injection: bool = False, evidence: str = "none",
                   local_contradiction: bool = False, impact_scope: str = "low",
                   active_target: bool = True,
                   recently_resolved: bool = False) -> dict:
    """给一条冲突打分，返回 {score, priority, penalties, signals}。纯函数，无 DB 依赖。"""
    score = 0
    penalties: list[str] = []
    signals: dict = {}

    if correction_intent:
        score += 20
        signals["correction_intent"] = 20
    if rag_score is not None:
        points = rag_points(rag_score)
        score += points
        signals["rag_score"] = f"{rag_score:.2f}→{points}"
    if recent_injection:
        score += 20
        signals["recent_injection"] = 20
    ev = EVIDENCE_POINTS.get(evidence, 0)
    score += ev
    signals["evidence"] = f"{evidence}→{ev}"
    if local_contradiction:
        score += 10
        signals["local_contradiction"] = 10
    im = IMPACT_POINTS.get(impact_scope, 0)
    score += im
    signals["impact_scope"] = f"{impact_scope}→{im}"

    if not active_target:
        score -= 25
        penalties.append("archived_only_target")
    if evidence == "none":
        score -= 20
        penalties.append("missing_evidence")
    if recently_resolved:
        score -= 25
        penalties.append("recently_resolved_same_pair")

    score = max(0, min(100, round(score)))
    if score >= 75:
        priority = "high"
    elif score >= 55:
        priority = "normal"
    elif score >= 35:
        priority = "idle"
    else:
        priority = "none"
    # 兜底：目标已归档、或完全没有证据 → 永不打扰用户
    if not active_target or evidence == "none":
        priority = "none"
    elif priority == "none":
        # 没触发任何硬排除，只是分低 —— 归为 idle（排最后，但仍然会问），
        # 不隐藏。理由：我们的提取管道拿不到"检索相关度/刚注入过"这类信号，
        # 分数天然偏低，若照搬 35 分卡，几乎所有真实冲突都不会问用户。
        priority = "idle"
    return {"score": score, "priority": priority, "penalties": penalties,
            "signals": signals}
