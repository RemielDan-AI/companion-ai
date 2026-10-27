"""记忆组装层：把**原始记忆**变成 4B 能安全使用的**自然语言上下文**。

## 为什么要有这一层

今天之前，"检索结果 → 注入提示"是一段**流水式的事实列表**：

    - 对方的事实：妹妹过敏的是花生；用户上周买了一次花生
    - 你们聊过：妹妹对花生过敏

这有三个毛病，实测都咬过人：

    ① **不回指**：库里存的是「林小雨」，用户问的是「小雨」——虽然检索层
       会用别名扩展去**找**，但**显示出来的文本里还是「林小雨」**，
       模型看不到"这两个是同一个"。
    ② **不分信不信**：抽取会出错（主体错位、缺宾语、空轮硬凑），
       而这些错误和正确记忆**长得一模一样**地塞进去。
    ③ **不标冲突**：新旧两条矛盾的记忆并排躺着，谁也不知道该信哪个。

本层由**程序主导**——模型（2B）只负责抽取，**不参与组装决策**。

## ⚠ 关于"置信度"：**没有用模型自报的 conf**

设计原意是"2B 抽取时带 conf，程序按 conf 分层"。**实测推翻了它**
（`tests/analysis/conf_signal_probe.py`）：

    分布：84% 都 ≥0.9；只有 4 个取值 {0.8,0.9,0.95,1.0}
    不区分好坏：「小雨是广州大学的学生」（主体错）→ 0.90~1.00
                「嗯，还行吧」（空轮，没什么可抽）→ 0.80~0.90，还抽出 3 条
    而且**为了加这个字段改成 items 结构，`kind` 直接坏掉**——
    模型把枚举说明 `'fact/preference/relation'` 原样抄了回来。

拿它分层 = 什么都没滤掉，而且**看起来还在正常工作**，最难发现。

改用**程序可算、可验证**的信号（`confidence()`）：

    接地强度（内容有多少二元组真的出现在用户原话里）　权重 0.7
    证据数（重复说过几次）　　　　　　　　　　　　　权重 0.3
    推理距离（跳进来的再打折）

实测分离度：
    「妹妹叫林小雨」     ← 原话有   → 接地 1.00 → 0.80　事实
    「用户是广州大学学生」← 有改写   → 接地 0.83 → 0.68　**可能**（可疑的降级了）
    「用户觉得还行」     ← 空轮     → 接地 0.33 → 0.33　**丢弃**

## 组装步骤

    ① 实体归一化 + 别名扩展：问「小雨」→ 认成「林小雨」
    ② 图遍历：从实体出发走关系边（用户 --妹妹--> 林小雨 --就读于--> 广州大学）
    ③ 置信度分层：事实(≥0.75) / 可能(0.5~0.75) / 丢弃(<0.5)
    ④ 冲突标注：同一实体新旧两个值 → 明确写"存在冲突"
    ⑤ 自然语言化：转成一段话，**不塞 JSON/三元组**
    ⑥ 长度控制：按（相关性 + 新鲜度 + 置信度）排序后截断
"""
from __future__ import annotations

import os

# 分层阈值。刻意留了中间档：抽取错的东西往往**不是全错**，
# 直接砍掉会丢记忆，标成"不确定"让模型自己斟酌更稳。
FACT_MIN = 0.75
POSSIBLE_MIN = 0.50

W_GROUND = 0.7          # 接地强度权重
W_EVIDENCE = 0.3        # 证据数权重
HOP_PENALTY = 0.85      # （已停用：见 confidence 的说明——hop 不掺进置信度）
SLOT_MAX = 4            # 槽位扫描最多补几条（限量防挤爆预算）

MAX_HOPS = 2
MAX_FACTS = 6           # 渲染时最多几条（预算之外再加一道上限，防一条超长）

_SUBJECT_PREFIXES = ("对方提到的人：", "对方", "用户")


def _body(content: str) -> str:
    """剥掉主体前缀，看内容本身。"""
    text = str(content or "").strip()
    for pfx in _SUBJECT_PREFIXES:
        if text.startswith(pfx):
            return text[len(pfx):].strip()
    return text


def _bigrams(text: str) -> set[str]:
    t = str(text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)
            if all("\u4e00" <= ch <= "\u9fff" for ch in t[i:i + 2])}


def grounding(content: str, user_text: str) -> float:
    """接地强度：内容里的二元组有多少**真的出现在用户原话里**（0~1）。

    ⚠ 这是本层置信度的主信号，也是**不依赖模型自报**的那条。
      空轮硬凑出来的内容天然得低分（「用户觉得还行」vs「嗯，还行吧」→ 0.33），
      而正常抽取接近 1.0。
    """
    body = _body(content)
    if not body:
        return 1.0
    text = str(user_text or "")
    if not text:
        return 1.0
    # ⚠ **必须字级 + 二元组混合**（实测踩过）：
    #   只按二元组算的话，抽取**一改写**就被判低分——
    #   「在广州大学上学」被抽成「就读于广州大学」，二元组只剩 5/9 = 0.56
    #   （`雨就/就读/读于/于广` 全是新的）→ 正常记忆被误判成低置信、进不了上下文。
    #   混进字级覆盖率后就稳了（同一个例子 0.63）。
    #   而该低分的照样低：「用户觉得还行」vs「嗯，还行吧」只有 0.33。
    grams = _bigrams(body)
    g_bigram = (sum(1 for g in grams if g in text) / len(grams)) if grams else 1.0
    chars = [ch for ch in body if "\u4e00" <= ch <= "\u9fff"]
    g_char = (sum(1 for ch in chars if ch in text) / len(chars)) if chars else 1.0
    return round(0.5 * g_char + 0.5 * g_bigram, 3)


def _source_utterance(memory, entry: dict) -> str:
    """这条记忆**记下来时**那一轮的"用户原话"。

    ⚠ 这很关键：置信度是"**记的时候**有多可信"，不是"跟当前提问有多相关"。
      第一版拿**当前 query** 去算接地 → 全部 0 分 → 整层输出空
      （实测踩过：问「小雨在哪上学」时库里的记忆跟这句话字面当然对不上）。
      库里存了 `source_episodic` 就是为这个。
    """
    eid = int(entry.get("source_episodic") or 0)
    if not eid:
        return ""
    try:
        row = memory.entry("episodic", eid) or {}
    except Exception:
        return ""
    return str(row.get("user_text") or "")


def confidence(entry: dict, memory=None, user_text: str = "") -> float:
    """一条记忆的置信度（0~1）。**完全由程序算**，不看模型自报。

    主信号是**接地强度**——拿"记下来时那轮的原话"比。
    取不到原话（老数据没链到情景）就**不打折**（`_grounded` 在入库时已经查过一遍，
    这里只是再补一刀更细的）。

    ⚠ **这里不掺"跳了几跳"**（2026-10-16 改）：
      第一版把距离乘进来（`*0.85`），结果**三跳题找回来的正确答案**
      （`楼下那家便利店叫全家`）被压成"不太确定"，模型就开始含糊其辞——
      而它正是要找的东西。
      两个轴不能混：**置信度说"这条可不可信"，距离说"离问题多远"**。
      距离只用来**排序**（见 `assemble()`），不改分层。
    """
    src = ""
    if memory is not None:
        src = _source_utterance(memory, entry)
    if not src:
        src = str(user_text or "")
    g = grounding(entry.get("content") or "", src) if src else 1.0
    ev = int(entry.get("evidence_count") or 1)
    conf = W_GROUND * g + W_EVIDENCE * min(1.0, ev / 3.0)
    return round(max(0.0, min(1.0, conf)), 3)


def tier_of(conf: float) -> str:
    if conf >= FACT_MIN:
        return "fact"
    if conf >= POSSIBLE_MIN:
        return "possible"
    return "drop"


def entity_seeds(user_text: str, memory) -> list[str]:
    """查询里提到的人/物 → **归一化后的正名**（「小雨」→「林小雨」）。

    这是"实体归一化"那一步：别名表知道 小雨↔林小雨，
    于是后面图遍历是**从一个人出发**，而不是从两个字符串分别出发。
    """
    from cognition.alias import canonical_of
    from cognition.terms import is_name_like
    out: list[str] = []
    text = str(user_text or "")
    names: set[str] = set()
    try:
        for item in memory.all_semantic(status="active"):
            body = _body(item.get("content") or "")
            for part in body.replace("，", "（").split("（"):
                part = part.strip("） 　(")
                if is_name_like(part):
                    names.add(part)
        from cognition.alias import all_aliases
        for row in all_aliases(memory):
            if row.get("alias"):
                names.add(str(row["alias"]))
    except Exception:
        pass
    for name in sorted(names, key=len, reverse=True):
        if len(name) >= 2 and name in text:
            canon = canonical_of(memory, name)
            if canon not in out:
                out.append(canon)
    return out


def relation_triples(memory) -> list[dict]:
    """把 `名字（关系）` 抽成三元组：`用户 --妹妹--> 林小雨`。

    ⚠ 主体统一是**用户**——这些关系记的是"用户提到的谁"。
      「妹妹」这类**关系词本身也是可用的锚**（问「我妹妹」就能落到林小雨）。
    """
    from cognition.terms import PAIR_RE
    out: list[dict] = []
    try:
        items = list(memory.all_semantic(status="active"))
    except Exception:
        return out
    for item in items:
        body = _body(item.get("content") or "")
        for m in PAIR_RE.finditer(body):
            obj, rel = m.group(1).strip(), m.group(2).strip()
            if obj and rel and obj != rel:
                out.append({"subject": "用户", "relation": rel, "object": obj,
                            "id": item.get("id")})
    return out


def entities_in(content: str, known: set[str]) -> list[str]:
    """这条记忆里提到了哪些**已知实体**（用于多跳扩展）。

    ⚠ 只认 `known` 里的实体，不认任意名字——否则每一条事实都会引出
      「对方/用户」这类词，frontier 会炸开。
    """
    text = str(content or "")
    out: list[str] = []
    for name in sorted(known, key=len, reverse=True):
        if len(name) >= 2 and name in text and name not in out:
            out.append(name)
    return out


def distinctive_terms(content: str, df: dict, total: int,
                      max_ratio: float = 0.34) -> set[str]:
    """这条记忆里**有辨识度**的二元组（用来连边）。

    ⚠ 为什么要"有辨识度"：随便一个二元组都拿来连边的话，
      全部记忆会连成一团、跳出去一堆无关的。
      只留出现次数不多不少的（1~34% 的记忆里有）：
      太常见的（「对方」「用户」）没有区分力，只出现一次的最有指向性。
    """
    if total <= 0:
        return set()
    cap = max(2, int(total * max_ratio))
    out = set()
    for g in _bigrams(content):
        n = df.get(g, 0)
        if 1 <= n <= cap:
            out.add(g)
    return out


def walk(memory, seeds: list[str], user_text: str = "",
         max_hops: int = MAX_HOPS, extra_entries=None,
         exclude_ids: set[str] | None = None) -> dict:
    """从种子出发走多跳，收集相关记忆。

    **两条通道**，缺一不可（2026-10-16 补，实测三跳题逼出来的）：

      通道① **实体链**：只要一条记忆提到了 frontier 里的实体，
        就把这条记忆里出现的其它已知实体加进下一跳。
      通道② **词共现链**：从已命中的记忆里取**有辨识度的二元组**，
        再去找也含这些词组的记忆。

    ⚠ 通道②不是锦上添花，是**必需的**。三跳题「我平时在哪买花生」的链路是
        花生 → （楼下那家）便利店 → 全家
      中间那两个都是**普通名词、不是实体名**，走实体图永远接不上
      （实测：只走实体链时 `走过=['林小雨','妹妹']`，便利店/全家一个没到）。
      而 `便利店` 这个二元组同时出现在两条记忆里 → 通道②一下就接上了。

    `extra_entries` 给检索已经命中的条目当起点——查询里可能压根没有实体名
    （「我平时在哪买花生」没有任何已知实体），这时全靠检索那边起的头。
    """
    from engine.retrieve import known_names

    entries: list[dict] = []
    seen_ids: set[int] = set()
    triples = relation_triples(memory)
    try:
        known = {n for n in known_names(memory) if len(n) >= 2}
        all_items = list(memory.all_semantic(status="active"))
    except Exception:
        known, all_items = set(), []

    # ⚠ **必须尊重"最近注入过就排除"**（2026-10-19 修的真 bug）：
    #   以前这里直接扫全库，把 `retrieve(exclude_ids=…)` 的排除**整个绕过了**——
    #   实测：第 2 轮检索已经排除到 0 条，但**块和第 1 轮一字不差**（同为 174 字）。
    #   于是同一条记忆**每一轮都被原样重灌**，用户重复提问时模型就反复看到同一批
    #   带多个主体的句子 → 主体越搞越乱。
    #   核心事实（过敏/名字/城市这类）在 app.py 那边**不参与排除**，这里照旧放行。
    if exclude_ids:
        all_items = [it for it in all_items
                     if f"semantic#{it.get('id')}" not in exclude_ids]

    # 二元组的文档频率（通道②要用）
    df: dict[str, int] = {}
    for item in all_items:
        for g in _bigrams(item.get("content") or ""):
            df[g] = df.get(g, 0) + 1
    total = len(all_items)

    visited: set[str] = set(seeds)
    frontier = [s for s in seeds if s]
    paths: list[str] = []
    terms: set[str] = set()
    # 起点：检索已命中的条目（查询里没有实体名时也能起步）
    for item in (extra_entries or []):
        iid = int(item.get("id") or 0)
        if iid not in seen_ids:
            seen_ids.add(iid)
            entries.append(dict(item, hop=0, why="检索命中"))
        terms |= distinctive_terms(str(item.get("content") or ""), df, total)

    for hop in range(max_hops + 1):
        if not frontier and not terms:
            break
        nxt: list[str] = []
        for entity in frontier:
            # 关系边（`用户 --妹妹--> 林小雨`）
            for tri in triples:
                if tri["relation"] == entity or tri["object"] == entity:
                    other = (tri["object"] if tri["relation"] == entity
                             else tri["relation"])
                    if other and other not in visited and other not in nxt:
                        nxt.append(other)
                    if hop == 0:
                        paths.append(f"用户 --{tri['relation']}--> {tri['object']}")
        # 通道①（提到 frontier 实体）+ 通道②（与本轮词组共现）一起扫
        for item in all_items:
            iid = int(item.get("id") or 0)
            content = str(item.get("content") or "")
            hit_entity = next((e for e in frontier if e and e in content), "")
            hit_terms = distinctive_terms(content, df, total) & terms
            if not hit_entity and not hit_terms:
                continue
            if iid not in seen_ids:
                seen_ids.add(iid)
                why = (f"提到「{hit_entity}」" if hit_entity
                       else f"共现词「{'、'.join(sorted(hit_terms)[:2])}」")
                # ⚠ hop 要**如实标**，它直接决定置信度打折：
                #   直接提到**种子**的算 0（等于直接命中）；
                #   靠扩展或共现找到的算 ≥1（`confidence` 会按 HOP_PENALTY 打折）。
                #   第一版全标 0 → 跳了三四道的东西跟直接命中同等待遇（实测踩过）。
                direct = bool(hit_entity) and hit_entity in seeds
                entries.append(dict(item, hop=(0 if direct else hop + 1), why=why))
            terms |= distinctive_terms(content, df, total)
            for other in entities_in(content, known):
                if other not in visited and other not in nxt:
                    nxt.append(other)
        visited.update(nxt)
        frontier = nxt

    return {"entities": [e for e in visited if e],
            "entries": entries, "triples": triples,
            "paths": list(dict.fromkeys(paths))[:6]}


def _fmt(content: str, seeds: list[str], memory) -> str:
    """把内容里的人名**补上别名**，让模型看得出是同一个人的两种叫法。

    ⚠ 这是第 ① 条"实体归一化"**在显示层**的落点：
      检索层已经用别名扩展**找**到了，但显示的还是「林小雨」——
      用户问「小雨」时模型认不出这是同一个人。

    ⚠ 别名要**插进已有的括号里**，别硬塞在前面：
      `林小雨（妹妹）` 硬插成 `林小雨（小雨）（妹妹）` 很难看、也容易被误读成两个属性。
    """
    import re as _re
    from cognition.alias import aliases_of
    text = _body(content)
    for seed in seeds:
        if not seed or seed not in text:
            continue
        nick = sorted(aliases_of(memory, seed))
        if not nick:
            continue
        joined = "、".join(nick[:2])
        # 已经处在 `名字（…）` 里 → 并进那个括号
        pat = _re.compile(_re.escape(seed) + r"（([^）]*)）")
        if pat.search(text):
            text = pat.sub(lambda m: f"{seed}（{m.group(1)}、{joined}）"
                           if joined not in m.group(1) else m.group(0),
                           text, count=1)
        else:
            text = text.replace(seed, f"{seed}（{joined}）", 1)
    return text


def spotlight(user_text: str, entries, limit: int = 2) -> list[str]:
    """⚠ **试过、有害、已停用**（2026-10-16）。

    想法很直白：失败案例里答案就在块里、模型却说"你没说过"，
    那就把"最像在回答你"的那条**顶到最前面**，弱模型总该看一眼。

    **实测把它推翻了（上限探针 61% → 42%）**：它按**查询字面重合**排序，
    而**多跳题的答案按定义就字面重合低**——于是它系统性地
    **把错的顶到最显眼的位置**：

        问「我平时在哪买花生」
        与这个问题最相关：林小雨对花生过敏；从楼下那家便利店买的花生。
        ↑ 顶到最前的是"过敏"（因为共享「花生」），
          而真正的答案「全家」跟问题零重合，反而排不上

    教训：**"把最相关的顶到前面"这件事检索已经做了**，
    再按同一个口径做一遍，只会加重同一个偏差、还多烧预算。
    这个函数留着是为了记住它，不要再接回去。
    """
    return []


def _clean_relations(rels: list[str]) -> list[str]:
    """清理「已认识的人」那行：去重、去掉被更完整的条目覆盖的裸名。

    ⚠ 实测那行会变成
        `林小雨（妹妹）；全家便利店（楼下）；林小雨；便利店（买的地点）`
      ——「林小雨」裸名重复、`便利店（买的地点）` 也没啥用。
      噪音会把答案淹掉，还会让模型以为有好几个不同的实体。
    """
    seen_name: set[str] = set()
    out: list[str] = []
    # 带关系的排前面（信息量更大），裸名后面来
    ordered = sorted(rels, key=lambda r: 0 if "（" in r else 1)
    for r in ordered:
        name = r.split("（")[0].strip()
        if not name or name in seen_name:
            continue
        seen_name.add(name)
        out.append(r)
    return out


def render(memory, entries, seeds, conflicts, user_text: str,
           max_chars: int = 380, agg: dict | None = None,
           hot: list[str] | None = None, hint: str = "") -> str:
    """渲染成**一段自然语言**。

    ⚠ 刻意不塞 JSON / 三元组：4B 对自然语言更敏感，
      格式标记（花括号、引号、字段名）会变成干扰，
      实测模型会把格式当内容复述出来（提示词里写 `**加粗**` 就被抄进记忆过）。
    """
    facts, possible, rels, themes, chats = [], [], [], [], []
    for e in entries:
        kind = str(e.get("kind") or "")
        conf = float(e.get("conf") or 0)
        text = _fmt(e.get("content") or "", seeds, memory)
        if e.get("theme"):
            themes.append(text[:24])
        elif kind == "relation":
            rels.append(text)
        elif kind == "episode" or e.get("summary"):
            chats.append(str(e.get("summary") or text)[:24])
        elif conf >= FACT_MIN:
            facts.append((conf, text))
        elif conf >= POSSIBLE_MIN:
            possible.append((conf, text))

    facts.sort(key=lambda x: -x[0])
    possible.sort(key=lambda x: -x[0])
    lines: list[str] = []

    # ⚠ 聚合结果**放最前面、单独一句**：它在回答"几个/哪些"这类问题时
    #   就是**答案本身**，混在事实列表里模型还得自己数一遍——那正是它做不到的事。
    if agg and agg.get("text"):
        lines.append(agg["text"])
    # 非计数槽位提问：程序**把答案拟好**放在最前，并明确标成"直接回答"。
    # 弱模型不会自己去块里找料（实测"送到没用"高达 88%），
    # 那就把它该说的那句话直接递到手上。
    if hint:
        lines.append("【直接回答】" + hint)
    # 最贴题的那几条**顶到最前**并明确标注：弱模型不会自己去块里找（实测见函数注释）
    if hot:
        lines.append("与这个问题最相关：" + "；".join(hot[:2]) + "。")
    # 「已认识的人」单独一行：关系和事实读法不同，
    # 混在一起会变成"林小雨（妹妹）"当事实念（实测很难读）。
    if rels:
        rels = _clean_relations(rels)
        lines.append("已认识的人：" + "；".join(rels[:4]) + "。")
    if facts:
        lines.append("已知：" + "；".join(t for _c, t in facts[:MAX_FACTS]) + "。")
    if possible:
        # ⚠ **改成提问口吻**（2026-10-18，用户提的思路）。
        #   原来写「不太确定（…**不要说得像确定的**）」——实测那句话是在**教模型否认**：
        #   三跳题里答案恰恰被放进这一档，模型于是答"你没说过哪家的"。
        #   换成"可以顺口问一下"：既承认不确定，又给出**一个动作**，
        #   而不是一个禁令。用户回答后由 `cognition/clarify.py` 关联回这几条记忆。
        lines.append("（这几条我不太确定，可以顺口问一下对方，别当成已确认的）"
                     + "；".join(t for _c, t in possible[:3]) + "。")
    if conflicts:
        # ⚠ 措辞要**给出处置**，不只是报警（2026-10-16 改）：
        #   原来写「别当成确定的事，可以自然地问一下」——弱模型读到就含糊，
        #   而这类冲突**恰恰有明确答案**（状态变更：谁新谁算数）。
        #   程序既然已经把新旧排出来了，就直接告诉它怎么办。
        lines.append("⚠ 同一件事有两种说法：" + "；".join(conflicts[:2]) +
                     "。一般以**新的为准**，旧的可当历史。")
    if themes:
        lines.append("长期印象：" + "；".join(themes[:2]) + "。")
    if chats:
        lines.append("你们聊过：" + "；".join(chats[:2]) + "。")

    if not lines:
        return ""
    # ⚠ 结尾那句原来一直是「**不确定的不要说得像确定的**」——可能是"送到了模型不用"的主因：
    #   它是一条**劝退指令**，而抽取器的工作就是改写（`花生是我从楼下那家便利店买的`
    #   → `上周从楼下那家便利店买了花生`），于是**几乎每条记忆都不"确定"**；
    #   模型读到的净效果是"这些别当真" → 直接答"你没说过"。
    #   实测证据：三跳题的答案就在块里，模型答"没说过哪家的"。
    #   `COMPANION_HEDGE=1` 可还原旧措辞，用来做**单变量 A/B**（只差这一句）。
    if os.environ.get("COMPANION_HEDGE") == "1":
        _tail = "**不确定的不要说得像确定的**）"
    else:
        # ⚠ 单变量 A/B 实测（2026-10-18，v1.3 3 次，**只差这一句**）：
        #   去掉「不确定的不要说得像确定的」→ 合计 **59% → 66%（+8）**，
        #   8 维里赢 6 维：多跳 56→67、计数 42→58、否定 67→83、状态变更 0→33、多主体 67→78。
        #   **那句劝退确实是"模型不肯用记忆"的主因之一。**
        #
        # ⚠ 但它**顺带**在压"别给太确定的医疗建议"——去掉后**安全边界 100% → 33%**。
        #   试过把它拆进记忆块（"记忆照实说 + 安全单列"）→ 合计掉到 **55%**：
        #   **指令一多，记忆那几维反而崩**（多跳 67→33）。说明这条路不对。
        #   → 安全改放在 `config/persona.yaml` 的 `bottom_lines`（常驻，且不污染记忆语义）。
        #   **记忆块只管事实，不兼管安全。**
        _tail = "记忆里记的就照实说，拿不准可以顺口问一句跟对方确认，不要凭空补细节）"
    header = ("（以下是关于对方的记忆：说的都是对方，不是你自己。"
              "回答时把对方称作「你」，不要用「我」来说对方的事。"
              "只在对方问起或确实相关时才提，不要每句都拿出来说，"
              "也不要说「根据我的记忆」。" + _tail)
    text = header + "\n" + "\n".join(lines)
    if len(text) > max_chars:
        # ⚠ 要给省略号留一格：`text[:max] + "…"` 会**超出上限 1 个字**
        #   （实测：预算 60 → 出来 61 字）。预算是有意义的硬约束，不能超。
        text = text[:max(0, max_chars - 1)].rstrip("；-，, 。") + "…"
    return text


def find_conflicts(entries, memory) -> list[str]:
    """未决冲突 → 人话，**把新旧都摆出来**。

    复用 `conflict_with`（入库时打的标记）。只摆事实、不下裁决——
    但**要明确谁新谁旧**，否则弱模型看到两个值只能含糊（实测：原措辞
    「别当成确定的事」会让它答"我不确定"，而正确答案就是新的那个）。
    """
    out: list[str] = []
    seen: set[int] = set()
    for e in entries:
        old_id = int(e.get("conflict_with") or 0)
        if not old_id or old_id in seen:
            continue
        old = memory.entry("semantic", old_id) or {}
        new_text = _body(e.get("content") or "")
        old_text = _body(old.get("content") or "")
        if new_text and old_text:
            out.append(f"新的「{new_text}」，旧的「{old_text}」")
            seen.add(old_id)
    return out


def assemble(user_text: str, memory, retrieved: dict,
             max_chars: int | None = None,
             exclude_texts: set[str] | None = None,
             session: str = "", turn: int = 0,
             exclude_ids: set[str] | None = None) -> dict:
    """**组装层入口**：检索结果 → 4B 可用的自然语言上下文。

    返回一份**可观测**的结果（便于测试与排查）：
        {"text", "facts", "possible", "dropped", "conflicts",
         "entities", "paths", "stats"}
    """
    if max_chars is None:
        try:
            from cognition import decay as decay_engine
            max_chars = int(decay_engine.load_rules().get("max_inject_chars", 380))
        except Exception:
            max_chars = 380

    seeds = entity_seeds(user_text, memory)
    # ⚠ 检索命中的条目要当**起点**交给 walk：
    #   查询里可能压根没有实体名（「我平时在哪买花生」一个实体都没有），
    #   这时全靠检索那边起的头，walk 才有的可走。
    walked = walk(memory, seeds, user_text,
                  extra_entries=(retrieved.get("semantic") or []),
                  exclude_ids=exclude_ids)
    by_id = {int(e.get("id") or 0): e for e in walked["entries"]}

    # **按槽位扫库**：问「住哪」就把 住在/搬到 的事实全捞出来。
    # 字面检索在这类问句上会整条落空（「我现在住哪」↔「对方搬到成都」零重合）。
    # 把检索层已经选出来的也并进来（它们可能不含种子实体，但相关）
    for item in (retrieved.get("semantic") or []):
        iid = int(item.get("id") or 0)
        if iid not in by_id:
            by_id[iid] = dict(item, hop=item.get("hop", 0),
                              why="检索命中" + ("（跳进来的）" if item.get("hop_from") else ""))

    # **按槽位扫库**：问「住哪」就把 住在/搬到 的事实全捞出来。
    #
    # ⚠⚠ **只在"字面检索整条落空"时才用**（2026-10-16 实测调出来的）：
    #   它的设计目标是「我现在住哪」↔「对方搬到成都」这种**零字面重合**——
    #   常规检索一条都返不回来，块是空的。
    #   第一版**无条件**跑它 → 上限探针 **61% → 57%（净负）**：
    #   检索本来找对了的时候，槽位扫描又倒进来一堆"同槽位但不相关"的事实，
    #   把预算挤掉、把答案淹掉。（多跳那维确实 67%→78% 涨了，
    #   但抵不上别处的损失。）
    #   所以加这道闸：**有检索结果就不扫**。
    if not by_id:
        scanned = slot_scan(user_text, memory, seeds + list(walked["entities"]),
                            exclude_ids=exclude_ids)
        if scanned:
            from cognition import decay as _decay
            _rules = _decay.load_rules()
            scanned.sort(key=lambda it: -_decay.rank_score(it, 0.5, _rules))
            for item in scanned[:SLOT_MAX]:
                iid = int(item.get("id") or 0)
                if iid not in by_id:
                    by_id[iid] = dict(item, hop=1)

    scored = []
    for e in by_id.values():
        conf = confidence(e, memory, user_text=user_text)
        scored.append(dict(e, conf=conf, tier=tier_of(conf)))
    # 排序：置信度优先，**同分时近的排前面**（跳数只影响次序，不改分层）
    scored.sort(key=lambda e: (-float(e.get("conf") or 0),
                               int(e.get("hop") or 0),
                               -int(e.get("id") or 0)))

    facts = [e for e in scored if e["tier"] == "fact"]
    possible = [e for e in scored if e["tier"] == "possible"]
    dropped = [e for e in scored if e["tier"] == "drop"]
    conflicts = find_conflicts(facts + possible, memory)
    # 聚合（计数/列举）走**全量扫描**，不走 top-k——排名天生只能带回几条，
    # 而"有几条"必须收全。识别 + 扫描都在程序侧。
    agg = aggregate(user_text, memory, seeds, walked["entities"])
    # 非计数的槽位提问：**把答案拟成一句可直接引用的话**（计数题已由聚合那句覆盖）。
    # ⚠ 这和失败的 spotlight 不同：它**不靠字面重合**，靠槽位扫全库把值算出来。
    #   详见 answer_hint 的注释（spotlight 按字面重合选，多跳题必然选错）。
    hint = "" if agg else answer_hint(user_text, memory, seeds,
                                       walked["entities"])
    hot = spotlight(user_text, facts + possible + dropped)
    text = render(memory, facts + possible, seeds, conflicts, user_text,
                  max_chars=max_chars, agg=agg, hot=hot, hint=hint)

    # **登记待确认的问题**：把上面那条"不太确定"变成可追踪的提问。
    # 用户下次回答后由 `cognition/clarify.py` 关联回这几条记忆（确认/否认/纠正）。
    # ⚠ 不叠问：已经有一个 open 的问题就先不问，免得一串问题轰用户。
    asked = {}
    if possible:
        try:
            from cognition import clarify as _clarify
            if not memory.open_question(session=session):
                g = group_of(user_text) or ""
                kws = _PREDICATE_GROUPS.get(g, ())
                values = []
                if g:
                    for e in possible[:2]:
                        v = _object_of(str(e.get("content") or ""), kws)
                        if v:
                            values.append(v)
                # ⚠ 槽位认不出时用**通用回引**（引用那条记忆本身去问）——
                #   否则大多数不确定记忆根本登记不上问题（实测：5 个槽位之外全落空）。
                qtext = _clarify.question_for(
                    g, values, next((s for s in seeds), "") or "你",
                    content=str(possible[0].get("content") or ""))
                if qtext:
                    qid = memory.ask_question(
                        [int(e.get("id") or 0) for e in possible[:2]],
                        g or "记忆", qtext, session=session, turn=turn)
                    asked = {"id": qid, "text": qtext, "topic": g or "记忆"}
        except Exception:
            asked = {}

    return {
        "text": text,
        "facts": facts,
        "possible": possible,
        "dropped": dropped,
        "conflicts": conflicts,
        "entities": seeds,
        "paths": walked["paths"],
        "aggregate": agg,
        "asked": asked,
        "stats": {
            "seeds": len(seeds), "walked": len(walked["entries"]),
            "fact": len(facts), "possible": len(possible),
            "dropped": len(dropped), "conflicts": len(conflicts),
            "aggregated": (agg or {}).get("count", 0),
            "chars": len(text), "budget": max_chars,
        },
    }


def enabled() -> bool:
    """开关（A/B 与排查用）。默认开。"""
    return os.environ.get("COMPANION_NO_ASSEMBLE") != "1"


# ---- 聚合：计数 / 列举（2026-10-16）----
# 上限探针里「我妹妹有几个忌口」「忌口都有什么」整维崩，原因是**召回不全**：
# top-k 检索天生只能带回两三条，而计数要求**把同类事实收全**。
# 这是排名解决不了的事——排名是按相关度取前 N，而计数要求"全部"。
# 所以放到组装层做**全量扫描**（程序侧，谁都不许掺和）。
_AGG_MARKS = ("几个", "多少", "哪些", "都有什么", "都有谁", "分别", "一共",
              "几种", "几项", "都哪些", "列举")

# 谓词分组：一个"槽位"下的多条事实才谈得上计数。
# ⚠ 只放**语义确实同槽**的词；放太宽会把不着边的事实一起数进来。
_PREDICATE_GROUPS: dict[str, tuple[str, ...]] = {
    "忌口": ("过敏", "不吃", "不能吃", "不敢吃", "忌口", "怕吃"),
    "喜好": ("喜欢", "爱喝", "爱吃", "爱好", "讨厌"),
    "住处": ("住在", "搬到", "住址", "搬家"),
    "工作": ("上班", "工作", "就职", "入职"),
    "学习": ("上学", "读书", "就读", "学校", "专业"),
}
# 问句里指代这些分组的词（用户不会说"忌口"两个字，得认同义）
_GROUP_HINTS: dict[str, tuple[str, ...]] = {
    "忌口": ("忌口", "过敏", "不能吃", "不能碰", "不能吃的"),
    "喜好": ("喜欢", "爱喝", "口味", "偏好"),
    "住处": ("住", "地址", "老家"),
    "工作": ("工作", "上班", "职业"),
    "学习": ("上学", "上学的地方", "学校", "专业", "读书"),
}


def _object_of(body: str, keywords) -> str:
    """从一条事实里抠出**槽位的值**（「对花生过敏」→ 花生）。

    ⚠ **必须在标点/连词处截断**（2026-10-18 实测）：原版取"关键词之后**全部**"，
      复合句就把整句尾巴都吞了：
          `对方住在南京并下个月要搬到成都` → 值 = `南京并下个月要搬到成都，`
      于是注入块里出现「对方的住处：南京并下个月要搬到成都，（最新）」
      —— **一句乱答案比不给更糟**，状态变更那一维当场从 67% 崩到 **0%**。
      在「并/和/跟/也/但/而/及」和标点处截断后，取到的是 `南京`。
    """
    import re as _re
    # ⚠ 先剥主体前缀（"对方林小雨对花生过敏" → "林小雨对花生过敏"）。
    #   不剥的话，`对(...)过敏` 会匹配到**「对方」里那个「对」**——
    #   实测被抠成 `方林小雨对花生`。调用方会先剥一次，这里再兜一层，
    #   保证传原始内容进来也不会错。
    body = _body(body)
    m = _re.search(r"对([^，。；、]{1,10}?)(?:过敏|不耐受)", body)
    if m:
        return m.group(1).strip()
    # 「在X上学/上班」——值在**关键词之前**，取"关键词之后"永远取不到（实测为空）
    m = _re.search(r"在([^，。；、]{2,10}?)(?:上学|读书|就读|念书|上班|工作)", body)
    if m:
        return m.group(1).strip()
    for kw in keywords:
        idx = body.find(kw)
        if idx < 0:
            continue
        rest = body[idx + len(kw):].strip("，。 ；、的了吧呢于")
        if not rest:
            continue
        # 截断：标点 + 连词（"南京并下个月…" → "南京"；"拿铁并且不吃糖" → "拿铁"）
        rest = _re.split(r"[，。；、！？,;!?]|并且|并|[和跟也但而及](?=.)",
                         rest)[0].strip("，。 ；、于")
        # 去掉动词前缀：`喜欢**喝**拿铁` → `拿铁`（值是宾语，不是动词短语）
        rest = rest.lstrip("喝吃穿玩看听去来")
        if rest:
            return rest[:12]
    return ""


def group_of(user_text: str) -> str:
    """问句问的是哪个槽位（住处/忌口/喜好/工作/学习）；认不出返回空串。"""
    text = str(user_text or "")
    for name, hints in _GROUP_HINTS.items():
        if any(h in text for h in hints):
            return name
    return ""


def slot_scan(user_text: str, memory, targets: list[str] | None = None,
              exclude_ids: set[str] | None = None) -> list[dict]:
    """**按问句的槽位扫库**——不靠字面重合。

    ⚠ 这是补一个大洞（2026-10-16 实测）：问「我现在住哪」，
      而库里存的是「对方搬到成都」/「对方住在南京」——
      **两者字面零重合**，BM25/二元组/向量全都命中不了，`retrieve` 返回空。
      于是块是空的，模型只能说"你没说过"。
      但程序**知道"住哪"问的是"住处"这个槽位**，可以直接把 `住在/搬到`
      的事实全捞出来——这是规则能做、检索排名做不到的事。

    `targets` 存了主体就只捞那个人的；空着就是"库里所有这槽位的事实"
    （单人库里都对，多人库里会被主体过滤兜住）。
    """
    group = group_of(user_text)
    if not group:
        return []
    keywords = _PREDICATE_GROUPS[group]
    let = [t for t in (targets or []) if t]
    out: list[dict] = []
    try:
        items = list(memory.all_semantic(status="active"))
    except Exception:
        return []
    for item in items:
        if str(item.get("kind")) == "relation":
            continue
        body = _body(item.get("content") or "")
        if not any(kw in body for kw in keywords):
            continue
        if let and not any(t in body for t in let):
            continue
        out.append(dict(item, why=f"按「{group}」槽位扫到"))
    return out


def _subject_pool(memory, seeds: list[str]) -> list[str]:
    """这个提问的**主体池**：种子 + 正名 + 与种子**有直接关系边**的实体。

    ⚠ 绝不能把图遍历碰到的所有实体都算进来（2026-10-18 组装指标抓到的 bug）：
      问「我妹妹对什么过敏」时，遍历会碰到「陈思远」（同事），
      把它也当合法主体 → 同事的海鲜过敏被算进妹妹头上 → **串人**。
      组装指标里的「多主体隔离」用例当场报红：`must_not=['海鲜']` 被违反。

      正确做法：只用
        ① 查询里的种子（妹妹）
        ② 种子的正名（别名表）
        ③ 与种子有**直接关系边**的（`用户--妹妹-->林小雨` → 林小雨）
      共现 ≠ 同一主体。
    """
    pool: list[str] = []
    for s in seeds or []:
        if not s:
            continue
        if s not in pool:
            pool.append(s)
        try:
            from cognition.alias import canonical_of as _canon
            canon = _canon(memory, s)
            if canon and canon not in pool:
                pool.append(canon)
        except Exception:
            pass
    try:
        triples = relation_triples(memory)
    except Exception:
        triples = []
    for tri in triples:
        if tri["relation"] in pool and tri["object"] not in pool:
            pool.append(tri["object"])
        if tri["object"] in pool and tri["relation"] not in pool:
            pool.append(tri["relation"])
    return pool


def _slot_rows(user_text: str, memory, seeds: list[str],
               walked_entities: list[str] | None = None,
               exclude_ids: set[str] | None = None,
               ) -> tuple[str, list[tuple[str, str]]]:
    """按问句槽位全量扫库，返回 `(槽位名, [(主体, 值), ...])`（按 id 新→旧）。

    ⚠ 主体池用 `_subject_pool`，**不用** `walked_entities`——
      后者是为了"找到"，不是为了"归属"。理由见 `_subject_pool` 的注释。
      `walked_entities` 参数保留只为兼容调用方，**不再参与归属判断**。
    """
    group = group_of(user_text)
    if not group:
        return "", []
    keywords = _PREDICATE_GROUPS[group]
    targets = _subject_pool(memory, seeds)
    try:
        all_items = list(memory.all_semantic(status="active"))
    except Exception:
        return group, []
    # 同上：槽位扫描也要尊重"最近注入过就排除"，否则它会把排除过的又捞回来
    if exclude_ids:
        all_items = [it for it in all_items
                     if f"semantic#{it.get('id')}" not in exclude_ids]
    all_items.sort(key=lambda it: -int(it.get("id") or 0))

    rows: list[tuple[str, str]] = []
    # ⚠ 主体要按**长度降序**匹配（2026-10-18 实测）：
    #   别名登记后 `known` 里同时有「小雨」和「林小雨」，而「小雨」是「林小雨」的**子串**，
    #   短的那个先匹配上就会把主体显示成「小雨」——用户问的明明是「林小雨」。
    targets = sorted(targets, key=len, reverse=True)
    for item in all_items:
        if str(item.get("kind")) == "relation":
            continue
        body = _body(item.get("content") or "")
        if not any(kw in body for kw in keywords):
            continue
        who = ""
        for t in targets:
            if t and t in body:
                who = t
                break
        if not who and targets:
            continue                          # 问的是某人，就别把别人的算进来
        # ⚠ 匹配到「小雨」要**映射回正名「林小雨」**（2026-10-18 实测）。
        #   走别名表：`canonical_of(memory, "小雨")` → "林小雨"。
        #   不这么做的话，注入块里会出现"小雨的忌口"，而用户问的是"林小雨"——
        #   模型会以为是两个人（这正是别名机制要消除的问题，不能自己再造一个）。
        if who:
            try:
                from cognition.alias import canonical_of as _canon
                who = _canon(memory, who) or who
            except Exception:
                pass
        val = _object_of(body, keywords)
        if not val:
            continue
        if (who, val) not in rows:
            rows.append((who, val))
    return group, rows


def answer_hint(user_text: str, memory, seeds: list[str],
                walked_entities: list[str] | None = None) -> str:
    """⚠ **试过、有害、已停用**（2026-10-18）。

    想法：计数题证明了"程序把答案直接说出来"有效（8B 上计数 0/12 → 6/12），
    那就推广到非计数的槽位提问——
        「我妹妹对什么过敏」→ 【直接回答】林小雨的忌口：花生
        「我现在住哪」      → 【直接回答】对方的住处：成都（最新）；另有 南京

    **实测把它推翻了**（上限探针，v1.3，3 次）：

        配置                合计          备注
        基线（无拟答案）     60/102 (59%)
        拟答案（取值有 bug） 55/102 (54%)
        拟答案（**取值修好**）**50/102 (49%)**  ← 修好了更差

    分维看也不一致：跨轮指代 50%→**100%**（好），但状态变更 67%→50%、
    多主体 78%→56%、否定 50%→**17%**（差）。**净损失 9 个点。**

    ## 为什么和"计数"不一样

    计数题的程序答案是**算出来的**（"一共 3 条：①②③"），客观、无歧义；
    而槽位的"直接答案"要**选一个值**——问「我现在住哪」，程序只能给出
    「成都（最新）；另有 南京」，**"最新/另有"这种判断对模型是干扰**，
    它反而更不会答了。加上那句 `【直接回答】` 会**压过**块里更完整的原句。

    ## 留着的价值：它顺带查出三个**真 bug**（都已单独修好并保留）

        ① 多值槽位被误判成冲突（`对方林小雨对花生过敏` ↔ `对海鲜过敏`）
           —— 会告诉模型"花生过期了"，下游合并还可能**真的把两条忌口合成一条**
        ② 主体池把"图遍历碰到的实体"也算进来 → **问妹妹带出同事的海鲜**（串人）
        ③ `_object_of` 取值把整句尾巴吞掉（`南京并下个月要搬到成都，`）

    ⚠ 不要再接回去。要用的话，得先解决"程序选值"这件事的**歧义**
    （什么时候该说"最新"、什么时候该把多个值都摆出来）。
    """
    return ""


def aggregate(user_text: str, memory, seeds: list[str],
              walked_entities: list[str] | None = None) -> dict | None:
    """识别"计数/列举"提问，并**全量扫描**同槽事实。

    返回 `{"group", "entities", "items", "count", "text"}`；不是聚合提问就返回 None。
    """
    text = str(user_text or "")
    if not any(mark in text for mark in _AGG_MARKS):
        return None
    group, rows = _slot_rows(user_text, memory, seeds, walked_entities)
    if not group or not rows:
        return None
    targets = [w for w, _v in rows if w] or list(seeds)
    vals = []
    for _who, val in rows:
        if val not in vals:                   # ⚠ 去重：同一件事说两次不该算两条
            vals.append(val)
    # 主体优先用"真正匹配上的那个"（可能是图遍历走到的正名）
    subject = next((w for w, _v in rows if w), targets[0] if targets else "对方")
    marks = "①②③④⑤⑥⑦⑧⑨"
    listed = "、".join(f"{marks[i] if i < len(marks) else ''}{v}"
                      for i, v in enumerate(vals))
    return {"group": group, "entities": targets, "items": vals,
            "count": len(vals), "subject": subject,
            "text": f"关于{subject}的{group}，记忆里一共 {len(vals)} 条：{listed}。"}



