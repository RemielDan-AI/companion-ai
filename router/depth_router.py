"""思考深度路由：按问题难度和情绪自动选档（L0 直觉 / L1 轻思 / L2 深思 / L3 深潜）。

纯规则实现——不额外调模型。理由：选档本身要毫秒级完成，
而且规则可解释（每档都能说清"为什么选它"），便于回溯调参。

打分规则：
  快答特征        -2      短句问事（吃什么、几点、叫什么）
  决策/两难       +2      要不要、该不该、纠结、值不值得…
  深度特征        +3      长期、人生、意义、利弊、帮我分析…
  情绪负面        +1      但纯倾诉封顶 L1（先接情绪，别讲道理）
  篇幅            +1/+2   >60 字 / >120 字
最终：≤0 → L0，1 → L1，2-3 → L2，≥4 → L3
"""
from __future__ import annotations

QUICK_MARKERS = [
    "今天吃什么", "吃点什么", "吃什么", "几点", "叫什么", "在吗", "睡了吗",
    "天气", "推荐个", "推荐一", "好了吗", "忙吗", "在干嘛", "干嘛呢",
]
DECISION_MARKERS = [
    "要不要", "该不该", "怎么选", "选择", "纠结", "权衡", "值不值得",
    "有没有必要", "下不了决心", "拿不定", "怎么决定", "选哪个",
]
DEPTH_MARKERS = [
    "长期", "一辈子", "人生", "未来", "规划", "意义", "利弊", "两难",
    "认真想", "帮我分析", "想不通", "反复想", "一直在想", "深层次",
]

LEVEL_NAMES = {"l0": "直觉", "l1": "轻思", "l2": "深思", "l3": "深潜"}


def _hits(text: str, markers: list[str]) -> list[str]:
    return [marker for marker in markers if marker in text]


def score(text: str, emotion_info: dict | None = None) -> tuple[int, list[str]]:
    text = text or ""
    reasons: list[str] = []
    value = 1  # 基线 L1

    quick = _hits(text, QUICK_MARKERS)
    if quick and len(text) <= 16:
        value -= 2
        reasons.append(f"快答特征（{quick[0]}）")

    decision = _hits(text, DECISION_MARKERS)
    if decision:
        value += 2
        reasons.append(f"决策/两难（{decision[0]}）")

    depth = _hits(text, DEPTH_MARKERS)
    if depth:
        value += 3
        reasons.append(f"深度特征（{depth[0]}）")

    if len(text) > 120:
        value += 2
        reasons.append("长描述")
    elif len(text) > 60:
        value += 1
        reasons.append("描述较长")

    info = emotion_info or {}
    if info.get("negative"):
        if decision or depth:
            value += 1
            reasons.append(f"情绪{info.get('emotion')}且涉及决策")
        else:
            # 纯倾诉：先接住情绪，不要长篇分析
            value = min(value, 1)
            reasons.append(f"纯倾诉（{info.get('emotion')}，强度 {info.get('intensity')}）")

    return value, reasons


def level_from_score(value: int) -> str:
    if value <= 0:
        return "l0"
    if value == 1:
        return "l1"
    if value <= 3:
        return "l2"
    return "l3"


def route_depth(text: str, emotion_info: dict | None = None) -> tuple[str, str]:
    """返回 (档位, 理由)。"""
    value, reasons = score(text, emotion_info)
    level = level_from_score(value)
    reason = "、".join(reasons) if reasons else "日常闲聊"
    return level, f"{reason} → 分值 {value}"
