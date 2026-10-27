"""拒绝决策与话术库。

四类拒绝（对应计划里的软拒绝/硬拒绝/转交/安全拒绝）：
- soft      软拒绝：能力没到，留个替代路径
- hard      硬拒绝：明确不做
- transfer  转交：该由专业的人来判断，我陪你准备
- safety    安全拒绝：涉及伤害自己或他人，优先把人拉回真实求助

技能 YAML 可以带 refusal.text 作为该技能的专属话术；没有就用这里的通用模板。
话术都用未央的口吻写：直接、有温度、不解释太多、不留客服腔。
"""
from __future__ import annotations

import random

KINDS = ("soft", "hard", "transfer", "safety")

# ⚠ 2026-10-27：话术搬去 `config/replies.yaml`（用户要求"改话术不用动代码"）。
#   保留 `_GENERIC` 这个名字是为了让已有调用点和测试不用改；
#   它的值在**每次读取时**从 `engine/replies.py` 现取（按 mtime 热加载）。
def _generic() -> dict[str, list[str]]:
    from engine import replies
    return {kind: replies.refuse_list(kind) for kind in KINDS}


class _GenericProxy(dict):
    """`_GENERIC` 的兼容外壳：读的时候永远拿最新话术。"""

    def __getitem__(self, key):
        return _generic()[key]

    def get(self, key, default=None):
        return _generic().get(key, default)

    def items(self):
        return _generic().items()


_GENERIC = _GenericProxy()

# 没有技能能接、又明显是在要一个"能力"时的默认拒绝（技能白名单原则）
# 内容见 config/replies.yaml 的 `no_skill`
# ⚠ 只作**兼容用的只读快照**（进程启动时取一次）。
#   真正说话要走下面 `no_skill()` —— 它**每次现取**，这样同一句话术不会反复出现。
#   （第一版这里直接让 `no_skill()` 返回这个常量，等于进程内永远同一句，
#    数组就白配了。）
NO_SKILL = __import__("engine.replies", fromlist=["get"]).get("no_skill")


def render(kind: str, text: str | None = None, reason: str = "") -> str:
    """按拒绝类型出话术；技能自带专属话术时优先用它。"""
    if text:
        return str(text).strip()
    kind = kind if kind in KINDS else "soft"
    return random.choice(_GENERIC[kind])


def no_skill(reason: str = "") -> str:
    """没有技能能接时的拒绝话术（**每次从数组里随机取一句**）。"""
    from engine import replies
    return replies.get("no_skill", NO_SKILL)


def describe(kind: str) -> str:
    return {
        "soft": "软拒绝（能力不足，留替代路径）",
        "hard": "硬拒绝（明确不做）",
        "transfer": "转交（交给专业人士）",
        "safety": "安全拒绝（优先真实求助）",
    }.get(kind, kind)
