"""技能执行器：能本地直接答复的技能实现。

- reminder：把要办的事落库并如实确认（定时触发留到后续阶段）
- llm / memory_recall：交给主流程的「检索 → 注入 → 生成」

新增函数型技能时，在这里加一个分支，或把执行器类型映射到新的函数。
"""
from __future__ import annotations

from cognition.memory import Memory

_PREFIXES = ("提醒我", "帮我记一下", "帮我记", "记一下", "记下来", "别忘记",
             "提醒一下", "到时候提醒我")


def run_local(decision, user_text: str, memory: Memory) -> tuple[str | None, str]:
    """返回 (直接答复, 工具查到的资料块)。

    - 直接答复不是 None：本地就能答完（提醒这类），不再走模型；
    - 资料块非空：工具查到了实时信息，交给生成流程用它来组织语言（避免编数据）。
    """
    executor = decision.executor or {}
    executor_type = executor.get("type")
    if executor_type == "reminder":
        return _reminder(memory, user_text), ""
    if executor_type == "tool":
        from tools.executor import run_tool
        tool = executor.get("tool") or {}
        result = run_tool(tool, text=user_text, skill_id=decision.skill_id,
                          skill_kind=decision.kind)
        if result.get("ok"):
            # ⚠ 2026-10-21 改：原来是
            #   「（刚查到的实时信息，照它说，别自己编：…）」
            # 这个括注**本身在邀请模型谈论它**——实测模型会照着说出来
            # （「20:19 周三——刚查到的实时信息」），还会衍生出
            # 「你上次没提过」这类元话语，进而触发无来源断言守卫把整句剪光。
            # 改成**中性的事实标记**：只陈述数据，不交代来源、不要求模型做什么。
            return None, f"【已知事实】{result['text']}"
        if result.get("blocked"):
            return None, ""          # 权限不允许：走技能自带的拒绝话术
        if result.get("fallback") and result.get("text"):
            return result["text"], ""
        return None, ""
    return None, ""


def strip_prefix(user_text: str) -> str:
    text = (user_text or "").strip()
    for prefix in sorted(_PREFIXES, key=len, reverse=True):
        if text.startswith(prefix):
            text = text[len(prefix):].strip("，,：:、 ")
            break
    return text or (user_text or "").strip()


def _reminder(memory: Memory, user_text: str) -> str:
    content = strip_prefix(user_text)
    memory.add_reminder(content)
    # 确认要办的事，但不引用对方原句（引用块会被当成学舌；这里只说记下的任务）
    return (
        f"好，我记下了：{content}。\n"
        "到点主动喊你这事我还没接上（定时提醒在后面的阶段做），"
        "不过你随时问「我记了什么事」，我翻给你看。"
    )
