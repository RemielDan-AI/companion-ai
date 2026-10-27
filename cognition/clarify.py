"""**把不确定的记忆变成一次提问，再把用户的回答关联回来**（2026-10-18）。

## 为什么要换这个思路

前面试过两次"把答案替模型算好"，都输了：

    spotlight（按字面重合顶到最前）      探针 61% → 42%
    拟答案 【直接回答】…                探针 59% → 49%（修好取值以后更差）

共同毛病：**它们在替用户下结论**。而记忆本来就不确定时，
替它下结论 → 模型要么照着说错话，要么干脆否认（"你没说过"）。

换法：**以不确定的口吻问出去**
    「我记得你好像提过住在成都，对吗？」
好处有两条：
    ① 不确定的提问**不会触发"你没说过"**——句子里已经带着"我记得好像"
    ② **用户的回答本身就是数据**：确认就升为确定，纠正就直接改对
       —— **不确定性自己会消失**，不用靠模型猜

## ⚠ 最大的风险：顺从偏差

如果我拿一个**错**的猜想去问，用户随口一个"嗯"，就把**错记忆升成了确定**。
那比不问还糟——**噪声被固化成事实**。

所以判据一律**从紧**：
    · 回答必须**短**（长句子多半是在讲别的事）
    · 必须有**明确的肯定/否定标记**
    · **否定优先**（"不对"里含有"对"，顺序反了就会把否认读成确认）
    · 有转折（但是/不过/其实）一律**不采信**
    · 拿不准就不动 —— 留着 `open`，过期自然作废

宁可漏采，不可错采：**漏采只是没学会，错采是学错了。**
"""
from __future__ import annotations

import json
import re

# 肯定标记 / 否定标记。**否定要先判**（"不对"含"对"）。
_YES = ("对", "是的", "是呀", "嗯", "没错", "记得", "确实", "有这回事", "应该是")
# ⚠ **不要放裸的「不」**（2026-10-18 实测）：`今天天气不错` 会被判成"否认并纠正"
#   ——「不错」是**肯定词**。只收成词的否定式。
_NO = ("不是", "不对", "没有", "没说过", "错了", "不记得", "忘了", "哪有",
       "记错", "搞错", "没这回事")
# 有转折 = 不是单纯在回答，不采信
_TURN = ("但是", "不过", "其实", "可是", "然而", "只是", "而且", "然后")
# 犹豫 = 还没给出答案（`嗯我想想` 是"让我想想"，不是确认）
_HEDGE = ("想想", "让我", "等等", "再说", "看看", "不知道", "不确定",
          "可能", "也许", "大概", "好像")


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~…]+", "", str(text or ""))


def judge_answer(user_text: str, max_len: int = 10) -> tuple[str, str]:
    """判断这句回答是**确认**、**否认**，还是**说不清**。

    返回 `("yes"|"no"|"corrected"|"", 理由)`。

    ⚠ 判据从紧，见模块注释（"宁可漏采，不可错采"）。
    ⚠ `yes` 的判据是**"剥掉肯定标记后几乎不剩什么"**，而不是"长度没超"：
      `嗯我想想` 长度 4 也不长，但剥掉「嗯」还剩「我想想」——那是**没给出答案**。
      实测按长度判会把它当确认。
    """
    raw = _norm(user_text)
    if not raw:
        return "", "空"
    if any(w in raw for w in _TURN):
        return "", "有转折，不是单纯回答"
    if any(w in raw for w in _HEDGE):
        return "", "还在犹豫，没给出答案"
    if any(w in raw for w in _NO):
        # 否认 + 还有别的内容 = 多半在纠正（新内容由正常抽取去记）
        rest = raw
        for w in _NO:
            rest = rest.replace(w, "")
        rest = rest.strip("的了啊呀吧呢哦")
        if len(rest) >= 2:
            return "corrected", f"否认并给出新说法：{str(user_text)[:40]}"
        return "no", "否认"
    if any(w in raw for w in _YES):
        rest = raw
        for w in sorted(_YES, key=len, reverse=True):
            rest = rest.replace(w, "")
        rest = rest.strip("的了啊呀吧呢哦")
        if len(rest) <= 2:                 # 剥完几乎不剩 = 单纯在确认
            return "yes", "明确确认"
        return "", f"除了确认还说了别的（{rest}），不采信"
    return "", "没有肯定/否定标记"


def resolve_pending(memory, user_text: str, session: str = "",
                    turn: int = 0) -> dict:
    """把本轮的回复**关联到上一轮那个待确认的问题**上，并据此更新记忆。

    返回 `{"question", "status", "ids", "detail"}`（没有待确认问题就返回 `{}`）。

    ⚠ 只认**更早轮次**问出去的问题（`before_turn`）——同一轮里提问紧接着就
      去解决的话，会把"触发提问的那句话"当成回答。
    """
    q = memory.open_question(session=session, before_turn=turn)
    if not q:
        return {}
    status, why = judge_answer(user_text)
    if not status:
        # 说不清 → **什么都不动**，问题继续挂着（到期自然作废）
        return {"question": q["id"], "status": "unresolved", "detail": why}
    try:
        ids = [int(x) for x in json.loads(q.get("semantic_ids") or "[]")]
    except Exception:
        ids = []
    if status == "yes":
        for sid in ids:
            memory.confirm_semantic(sid, why)
    elif status in ("no", "corrected"):
        for sid in ids:
            memory.reject_semantic(sid, why)
    memory.resolve_question(q["id"], status, user_text)
    return {"question": q["id"], "status": status, "ids": ids, "detail": why}


def question_for(topic: str, values: list[str], subject: str = "",
                 content: str = "") -> str:
    """把"不确定的记忆"说成一句**带疑问语气**的话（不是断言）。

    ⚠ 措辞要能体现出"我不太确定，你确认一下"：
      既不能像事实（那会让模型照着断言），也不能太虚（模型就不提了）。

    ⚠ **没有槽位时用"通用回引"**（2026-10-18 实测补充）：
      `group_of()` 只认 住处/忌口/喜好/工作/学习 五个槽位，
      而多数提问不属于这五个（「我平时在哪买花生」一个都不沾）。
      只认槽位的话，**大多数不确定记忆根本登记不上问题**。
      兜底做法：直接把那条记忆**引出来问**——不需要知道它属于哪个槽位。
    """
    who = subject or "你"
    v = "、".join([x for x in values if x][:2])
    if v and topic == "住处" or (v and topic == "工作") or (v and topic == "学习"):
        return f"我记得{who}好像是在{v}，对吗？"
    if v and topic == "忌口":
        return f"我印象里{who}对{v}过敏或是不吃，是这样吗？"
    if v and topic == "喜好":
        return f"我记得{who}喜欢{v}，没记错吧？"
    if v and topic:
        return f"我印象里{who}的{topic}是{v}，对吗？"
    # 通用兜底：直接引用那条记忆
    quote = str(content or "").strip()
    for pfx in ("对方提到的人：", "对方", "用户"):
        if quote.startswith(pfx):
            quote = quote[len(pfx):]
            break
    quote = quote.strip("，。；、")[:24]
    if not quote:
        return ""
    return f"我记得你好像提过「{quote}」，是这么回事吗？"
