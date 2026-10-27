"""冷启动策略（项目改进v1 改进 26）。

新用户不该被"预设"成某个人，所以：
- **不预设任何个人记忆**：库里空着就是空着，宁可先问也不猜；
- **前几轮主动问关键信息**：名字、怎么称呼、作息、在意的事——一轮问一件，不连珠炮；
- **逐步积累**：早期只如实记录对话，不急着写成画像；
- **第一周只记录、不推断价值观**：价值观靠长期养成，冷启动阶段不生成候选（allow_values）。

阶段划分（按对话轮数与首次对话时间）：
    全新（0 轮）→ 初期（≤10 轮或不到 1 天）→ 成长期（不到 7 天）→ 稳定（≥7 天）
"""
from __future__ import annotations

import datetime as dt
import pathlib

from cognition.memory import Memory, parse_time

VALUE_QUIET_DAYS = 7          # 第一周不推断价值观（可在 config/onboarding.yaml 改）
EARLY_TURNS = 3               # 开头这几轮算初期
EARLY_DAYS = 1

CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "onboarding.yaml"


def _reload_config() -> None:
    """允许用 config/onboarding.yaml 调整等待期（比如设为 0 立刻开始养价值观）。"""
    global VALUE_QUIET_DAYS, EARLY_DAYS
    if not CONFIG_PATH.exists():
        return
    try:
        import yaml
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if data.get("value_quiet_days") is not None:
        VALUE_QUIET_DAYS = float(data["value_quiet_days"])
    if data.get("early_days") is not None:
        EARLY_DAYS = float(data["early_days"])

# 关键信息：关键词 → 人话名称
KEY_INFO = {
    "名字": ("叫", "名字", "称呼"),
    "住处": ("住在", "城市", "老家"),
    "作息": ("睡", "早起", "熬夜", "作息"),
    "工作": ("工作", "上班", "职业", "上学"),
    "在意的事": ("喜欢", "讨厌", "不喜欢", "在意", "害怕"),
}
ASK_ORDER = ["名字", "在意的事", "作息", "工作", "住处"]


def _now() -> dt.datetime:
    return dt.datetime.now()


def stats(memory: Memory) -> dict:
    """冷启动的基本盘：多少轮、聊了几天、有哪些关键信息。"""
    episodes = memory.recent_episodes(limit=1000)
    turns = len(episodes)
    times = [parse_time(row.get("created_at") or "") for row in episodes]
    times = [t for t in times if t]
    first = min(times) if times else None
    days = (_now() - first).total_seconds() / 86400 if first else 0.0
    facts = " ".join(str(item.get("content") or "") for item in memory.all_semantic())
    known = [label for label, words in KEY_INFO.items()
             if any(word in facts for word in words)]
    missing = [label for label in ASK_ORDER if label not in known]
    return {"turns": turns, "days": round(days, 2), "known": known, "missing": missing,
            "first_at": first.isoformat(timespec="seconds") if first else ""}


def stage(memory: Memory) -> str:
    """当前处在哪个阶段。

    时间为主、开头几轮为辅：聊得再少，认识满一周也算稳定期；
    刚认识那几轮（或不到一天）算初期，中间算成长期。
    """
    info = stats(memory)
    if info["turns"] == 0:
        return "全新"
    if info["days"] < EARLY_DAYS:
        return "初期"          # 刚认识（哪怕第一天聊了很多轮，也算刚认识）
    if info["days"] < VALUE_QUIET_DAYS:
        return "成长期"
    return "稳定"


def allow_values(memory: Memory) -> bool:
    """第一周只记录、不推断价值观（改进 26 的硬要求）。"""
    info = stats(memory)
    if info["turns"] == 0:
        return False
    return info["days"] >= VALUE_QUIET_DAYS


def next_question(memory: Memory) -> str:
    """下一个该问的关键信息（一轮只问一件）。"""
    missing = stats(memory)["missing"]
    return missing[0] if missing else ""


def prompt_hint(memory: Memory) -> str:
    """给生成的提示：早期自然地问一句关键信息，别连珠炮、别写档案。"""
    current = stage(memory)
    if current in ("全新", "初期"):
        question = next_question(memory)
        if question:
            return (f"（你们刚认识不久：这轮可以自然地问一句对方的{question}，"
                    f"像朋友聊天那样带出来，不要连珠炮式提问，也不要提「档案」「记录」这种字眼。）")
        return "（你们刚认识不久：先顺着聊，别急着下判断。）"
    if current == "成长期":
        return "（认识没几天：可以自然地接着聊，不必刻意追问。）"
    return ""


def report(memory: Memory) -> str:
    """冷启动现状，给人看的一页。"""
    info = stats(memory)
    lines = [f"阶段：{stage(memory)}（聊了 {info['turns']} 轮、{info['days']:.1f} 天）",
             f"已经知道：{'、'.join(info['known']) or '（还什么都不知道）'}",
             f"还不知道：{'、'.join(info['missing']) or '（该知道的都知道了）'}"]
    if allow_values(memory):
        lines.append("价值观：已过第一周，可以在对话里慢慢养成了")
    else:
        left = max(0.0, VALUE_QUIET_DAYS - info["days"])
        lines.append(f"价值观：第一周只记录不推断（还差 {left:.1f} 天）")
    question = next_question(memory)
    if question:
        lines.append(f"下一次可以自然问一句：{question}")
    return "\n".join(lines)


_reload_config()      # 模块导入时读一次 config/onboarding.yaml
