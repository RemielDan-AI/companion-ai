"""情绪识别：固定标签 + 归一化 + 关键词兜底。

阶段 2 里给记忆库用（情景记忆要存情绪）；阶段 4 会扩展成完整的情绪识别模块
（用于自动选档和风格调整）。
"""
from __future__ import annotations

CANONICAL = ["平静", "开心", "低落", "疲惫", "焦虑", "生气", "孤独", "期待", "压力"]

# 关键词 → 标签；按关键词长度从长到短匹配，避免"天气"命中"气"
_SYNONYMS = [
    ("疲惫", "疲惫"), ("疲劳", "疲惫"), ("没力气", "疲惫"), ("提不起劲", "疲惫"),
    ("不想动", "疲惫"), ("不想干", "疲惫"), ("没劲", "疲惫"), ("累", "疲惫"),
    ("低落", "低落"), ("难过", "低落"), ("伤心", "低落"), ("沮丧", "低落"),
    ("失落", "低落"), ("郁闷", "低落"), ("委屈", "低落"), ("想哭", "低落"),
    ("难受", "低落"), ("不开心", "低落"), ("心里堵", "低落"), ("堵得慌", "低落"),
    ("焦虑", "焦虑"), ("烦躁", "焦虑"), ("担心", "焦虑"), ("紧张", "焦虑"),
    ("害怕", "焦虑"), ("不安", "焦虑"), ("心事", "焦虑"), ("烦", "焦虑"),
    ("憋", "焦虑"), ("透不过气", "焦虑"),
    ("生气", "生气"), ("愤怒", "生气"), ("火大", "生气"), ("气死", "生气"),
    ("孤单", "孤独"), ("寂寞", "孤独"), ("没人懂", "孤独"),
    ("开心", "开心"), ("高兴", "开心"), ("愉快", "开心"), ("兴奋", "开心"),
    ("期待", "期待"), ("盼", "期待"),
    ("压力", "压力"), ("加班", "压力"), ("忙", "压力"), ("deadline", "压力"),
    ("平静", "平静"), ("还行", "平静"), ("还好", "平静"), ("一般", "平静"),
]
_SYNONYMS.sort(key=lambda item: -len(item[0]))

NEGATIVE = {"低落", "疲惫", "焦虑", "生气", "孤独", "压力"}


def normalize(label: str | None) -> str:
    """把任意情绪描述（模型给的或用户的）归一到固定标签。"""
    if not label:
        return "平静"
    text = str(label).strip()
    for canonical in CANONICAL:
        if canonical in text:
            return canonical
    for keyword, tag in _SYNONYMS:
        if keyword in text:
            return tag
    return "平静"


def guess(text: str) -> str:
    """没有模型提取时的关键词兜底。"""
    if not text:
        return "平静"
    for keyword, tag in _SYNONYMS:
        if keyword in text:
            return tag
    return "平静"


def is_negative(label: str | None) -> bool:
    return normalize(label) in NEGATIVE


# 程度副词/强化表达：用来估情绪强度
_INTENSIFIERS = ["特别", "非常", "极其", "太", "很", "好", "死了", "爆", "疯了",
                 "受不了", "崩了", "撑不住", "要命"]
_STRONG = ["崩溃", "绝望", "撑不住", "受不了", "活不下去", "想死"]


def analyze(text: str) -> dict:
    """情绪识别：给深度路由和风格引擎提供情绪与强度。

    返回 {emotion, intensity(1-5), negative, matched}
    """
    text = text or ""
    emotion = guess(text)
    matched = [keyword for keyword, tag in _SYNONYMS if tag == emotion and keyword in text]

    intensity = 1
    if emotion != "平静":
        intensity = 2
    intensity += sum(1 for word in _INTENSIFIERS if word in text)
    if "！" in text or "!" in text:
        intensity += 1
    if any(word in text for word in _STRONG):
        intensity = max(intensity, 4)
    if text.count("！") + text.count("!") >= 2:
        intensity += 1

    return {
        "emotion": emotion,
        "intensity": max(1, min(5, intensity)),
        "negative": emotion in NEGATIVE,
        "matched": matched[:3],
    }
