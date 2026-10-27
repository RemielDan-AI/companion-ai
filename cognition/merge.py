"""记忆冲突的**模型合并**：把「旧说法 + 新说法」合成一条。

## 为什么要有这个

冲突检测的初衷就是**合并相关记忆**（用户的话），但原来的实现只做了「把新说法
藏起来等用户拍板」——而用户几乎不会回答，于是新说法**永久丢失**。
（"不隐身"那一层已经在 `memory.upsert_semantic` 修掉了；这里是**收尾**那一层。）

合并需要理解，不是规则能算出来的：

    旧：对方住在南京
    新：对方搬到成都
    → 合成：对方从南京搬到成都（现住成都）

## ⚠ 合并是**破坏性**操作，护栏必须硬

模型（尤其 2B）会编。所以这里的取向是**宁可不合并，也不写错**：

    ① **数字必须来自原文**——凭空出现数字直接拒（数字错比句子丑严重得多）
    ② **字符级接地**——合成句里的字必须都在两条原文里出现过，
       防止模型"造"出一个新名字
    ③ **长度上界**——不许比两条原文加起来还长一截
    ④ **主体必须在**——丢了"对方"就是把用户的事记成了别人/自己
    ⑤ **失败就保持未决**，两条都留着、都可检索；**绝不自动删旧条**
    ⑥ 全程 `log("merge", ...)` 留痕，事后能查"当时合成了什么"

模型不可用 / 被护栏拒 → 返回 `action="unresolved"`，**什么都不改**。
"""
from __future__ import annotations

import re

MERGE_SYSTEM = "你在合并两条关于同一个人的记忆，只输出合并后的一句话。"

MERGE_TEMPLATE = """同一个人先后说过两件事，请合并成一句最准确的话。

旧说法：{old}
新说法：{new}

要求：
- 如果新说法推翻了旧说法，就写出现在成立的那个，并保留变化过程。
- 如果两件事可以同时成立，就把它们都写进去。
- 只输出一句话，不要解释、不要引号、不要标点结尾以外的多余文字。
- 不要添加原文里没有的信息。"""

_NUM_RE = re.compile(r"\d+")

# 允许出现的**虚词/连接词**（原文没有也算合理）：
# 合并本来就需要「从…搬到…」「现在」「也/或」这类连接，全禁掉会把正常合并也拒了。
# 而**实词**（地名、人名用字，如「北京」的北/京）不在表里 →
# 模型想生造一个地点或人名就会被挡下，那正是最危险的编造。
_FUNCTION_CHARS = set(
    "从到现在或也并且还又就已会要到"
    "的是和与及但而前后先再更最很比"
    "因为所以被把让给对向往之其等如若"
    "了过起来去出上下里外中间时候"
    # ⚠ 下面这批是**实测补的**：真 2B 合并时会自然地用这些连接/副词，
    #   只收上面那批的话 6 条里要被误拒 4 条（曾/改/说/继续）。
    #   它们都不是地名/人名的构字，放行不会让编造蒙混过去——
    #   编造地点仍然会带出原文没有的实词（「北京」的北/京）。
    "曾改说继续开始变成保持原来以前后来"
    "依然仍然一直常常经常偶尔一般通常大致"
    "可能应该觉得认为表示提到称叫先把各自"
)


def _chars(text: str) -> set[str]:
    return {ch for ch in str(text or "") if "\u4e00" <= ch <= "\u9fff"}


def check_merge(merged: str, old: str, new: str) -> tuple[bool, str]:
    """护栏：合成句能不能信。返回 (通过, 原因)。纯函数，便于测。"""
    m = str(merged or "").strip().strip("“”\"'。")
    if not m:
        return False, "空"
    if len(m) < 3:
        return False, "太短"

    src = f"{old}{new}"

    # ① 数字必须来自原文
    for n in _NUM_RE.findall(m):
        if n not in src:
            return False, f"凭空出现数字「{n}」"

    # ② 实词接地：不许造出原文里没有的**实词用字**（防生造地点/人名）。
    #    虚词/连接词（从、现在、也、或…）放行——合并本来就需要它们。
    invented = _chars(m) - _chars(src) - _FUNCTION_CHARS
    if invented:
        return False, f"出现原文没有的实词用字「{''.join(sorted(invented))[:6]}」"

    # ③ 长度上界
    if len(m) > len(old) + len(new) + 10:
        return False, f"太长（{len(m)} > {len(old)}+{len(new)}+10）"

    # ④ 主体必须在（记忆里的主体统一是「对方」）
    if "对方" in src and "对方" not in m:
        return False, "丢了主体「对方」"

    return True, ""


def merge_pair(generator, old: str, new: str, verbose: bool = False) -> dict:
    """让模型合并两条，返回 {action, merged, reason}。

    action: `merged`（可用） / `unresolved`（模型不可用或被护栏拒，**什么都不改**）
    """
    if generator is None:
        return {"action": "unresolved", "merged": "", "reason": "没有可用的模型"}
    prompt = MERGE_TEMPLATE.format(old=old, new=new)
    try:
        raw = generator.complete(MERGE_SYSTEM, prompt,
                                 max_tokens=120, temperature=0.2)
    except Exception as exc:
        return {"action": "unresolved", "merged": "",
                "reason": f"模型不可用：{type(exc).__name__}"}
    merged = str(raw or "").strip().splitlines()[0].strip() if raw else ""
    merged = merged.strip("“”\"'。")
    ok, reason = check_merge(merged, old, new)
    if not ok:
        return {"action": "unresolved", "merged": "", "reason": f"护栏拒绝：{reason}"}
    return {"action": "merged", "merged": merged, "reason": ""}


def apply_merge(memory, new_id: int, old_id: int, merged: str) -> dict:
    """把合并结果落库：**新条改成合成句并标记已解析，旧条标 superseded（不删）**。"""
    from cognition.memory import now

    stamp = now()
    # ⚠ 记的是 `merged_into`（表里本来就有这一列，语义是"由谁合并而来"）。
    #   我第一版写成 `merged_from`，那一列不存在 → 会直接抛错。
    memory.conn.execute(
        "UPDATE semantic SET content=?, merged_into=?, conflict_with=0,"
        " resolver_priority='none', updated_at=? WHERE id=?",
        (merged, old_id, stamp, new_id))
    memory.conn.execute(
        "UPDATE semantic SET status='superseded', superseded_by=?, updated_at=?"
        " WHERE id=?", (new_id, stamp, old_id))
    memory.conn.commit()
    memory.log("merge", "semantic", int(new_id),
               f"合并 #{old_id}「{memory.entry('semantic', old_id).get('content','')[:24]}」"
               f" + #{new_id} → 「{merged[:34]}」")
    return {"action": "merged", "merged": merged, "old_id": old_id, "new_id": new_id}
