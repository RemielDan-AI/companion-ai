"""记忆检索：按当前输入找相关记忆，并组装成可注入系统提示的短文本。

三级来源：
1. 语义记忆（事实/偏好/关系）——向量检索为主，关键词兜底
2. 情景记忆（历史对话摘要）——向量检索为主，关键词兜底
3. 近几轮原文——不检索，直接带最近 N 轮（短期上下文）

排序用「相关性 + 重要度 + 新鲜度」综合打分（cognition/decay.py），
冷存储记忆默认不参与主动注入，但按需检索时仍可命中，命中即自动唤醒。
嵌入服务不可用时自动退化为关键词匹配，保证对话不中断。
"""
from __future__ import annotations

import os
import re

from cognition import decay as decay_engine
from cognition.bm25 import BM25, fuse, normalize
from cognition.memory import Memory
from cognition.terms import BARE_NAME_MAX, PAIR_RE, is_name_like
from cognition.vectors import EmbeddingUnavailable, VectorIndex, embed

# BM25 索引缓存：语料变了就重建。
# 键用「条数 + 最大 id + 内容指纹」，避免每轮都扫全库（记忆库会长到几千条）。
_BM25_CACHE: dict[str, object] = {}

# 词法分在「融合相关度」里的权重。
# 为什么给这么高：实测带向量 8/10、纯词法 1/10，词法侧本来就弱，
# 而向量侧对「专名/数字/罕见词」容易漏（「我对什么过敏」问不出「芒果」）。
# 归一化后词法分是 0–1，所以这个权重就是它在相关度里的占比上限。
LEXICAL_WEIGHT = 0.7

# 单条命中就给这个词法下限分。理由：只有一条记忆含「芒果」时，
# 按最大值归一化会得到 1.0 而虚高；但完全不认它，就退回旧的「全被闸门挡掉」。
LEXICAL_FLOOR = 0.3

# BM25 分的**绝对饱和**半饱和点：`lex = raw / (raw + K)`（2026-10-17）。
# ⚠ 原来用的是 `normalize(raw)`（按本次查询最大值归一）——那会让**任何查询里
#   词法第一的那条永远拿到 lex=1.0 → relevance ≥ 0.900**，把靠向量命中、
#   词法为 0 的真答案压死（37 题标尺里 7 道题的榜首是干扰句，相关性全是精确的 0.900）。
#   换成绝对饱和后：标尺 MRR **0.768 → 0.878**、榜首对 **25 → 30**，hit 不掉。
# K 的取值见标尺扫描：K=5 附近最优；K 再大就会被 `LEXICAL_FLOOR` 接管，
# 退化成"沾边就给 0.3"的平坦保底（标尺上结果一样，但少了区分度）。
LEX_SATURATION_K = 5.0

# ---- 一跳扩展（2026-10-15）----
# 为什么需要：多跳联想**实测是 0/3**（`tests/analysis/multihop_probe.py`）——
# 「我妹妹有什么忌口」找不到「林小雨不能吃花生」，因为查询里的「妹妹」
# 和答案记忆零词重叠，必须经「妹妹→林小雨」这一跳。
# 三条失败时排第一的**恰好都是第一跳**：入口找对了、走不到答案。
# 而走这一跳要的数据我们**已经存着了**：关系记忆的 content 形如
# 「对方提到的人：林小雨（妹妹）」，就是**别名→名字**映射
# （见 `cognition/extract.py:137` 与 `:156`）。
# 实测（`tests/analysis/hop_expansion_proto.py`）：多跳 0/3 → 2/3。
#
# 跳数衰减借自 VCPToolBox 浪潮算法的「有限脉冲响应」w_t = γ^t / Σγ^r：
# 第 t 跳的结果乘 γ^t，强调近跳、抑制远端（它默认 γ=0.60）。
HOP_DECAY = 0.6
HOP_MAX = 2                 # 最多跳几个目标：一轮别跳成一锅粥
# 锚点形态：`名字（关系）`。
# ⚠ 与 `cognition/terms.py` 共用同一个正则：原来这里写死 `{2,4}`，
#   会把「工业软件公司」**静默截断**成「软件公司」，锚点歪掉，后续检索跟着歪。
_HOP_RE = PAIR_RE
# 取锚点前要剥掉的主体前缀（与 `cognition/extract.py` 的 `_canonical` 约定一致）
_HOP_PREFIXES = ("对方提到的人：", "对方", "用户")

# 中文按二元组切分，英文数字按词切
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{2}")


def keywords(text: str, extra_words: set[str] | None = None) -> set[str]:
    """切词：**先把已知专名整词切出来**，剩下的再走二元组。

    为什么要这样：二元组会把"林小雨"切成"林小/小雨"，这两片都可能命中无关内容
    （"小雨"能对上天气、"林小"能对上别的名字）。把已认识的专名先整词切出来后，
    它才是一个有区分度的检索单位。
    """
    if not text:
        return set()
    text = str(text)
    words: set[str] = {w.lower() for w in re.findall(r"[a-zA-Z0-9]+", text) if len(w) > 1}
    for name in (extra_words or set()):
        name = str(name or "").strip()
        if len(name) >= 2 and name in text:
            words.add(name)                     # 整词保留
            text = text.replace(name, "　" * len(name))   # 挖掉，避免再被切碎
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    bigrams = {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}
    return bigrams | words


def known_names(memory) -> set[str]:
    """已知的专名：从关系记忆（"对方提到的人：林小雨（妹妹）"）和关联备注里收集。

    这些名字在检索时整词切分，不被二元组切碎。

    另加**别名**（2026-10-16）：别名表里的「小雨」也要算已知专名，
    否则它会被二元组切碎（`keywords()` 的整词保护失效），
    而且查询扩展也认不出它。
    """
    names: set[str] = set()
    try:
        for item in memory.all_semantic(status="active"):
            # ⚠ 不只 relation：实测模型对"该进 relations 还是 facts"的选择不稳定，
            #    同一句一次进 relations、一次进 facts。这里放宽到全部语义条目，
            #    否则「工业软件公司（上班的地方）」落在 facts 里时就不算已知专名，
            #    检索时会被二元组切碎（`keywords()` 的整词保护失效）。
            body = str(item.get("content") or "")
            for prefix in _HOP_PREFIXES:
                if body.startswith(prefix):
                    body = body[len(prefix):]
                    break
            for part in body.replace("，", "（").split("（"):
                part = part.strip("） 　(")
                if not is_name_like(part):
                    continue
                # ⚠ **不带括号的片段额外限长**（2026-10-16 补）：
                #   整句事实通常没有括号（`对方林小雨不吃香菜`），
                #   剥掉主体前缀后就是一长串，`is_name_like` 挡不住所有形态。
                #   而真名字很少超过 6 字。实测泄漏出过
                #   `林小雨不吃香菜`/`林小雨怕狗` 当实体，
                #   于是**图遍历把它们当节点去走**（走到=['妹妹','林小雨不吃香菜',…]）。
                #   带括号的仍可到 NAME_MAX（`北京字节跳动科技有限公司（单位）`）。
                if "（" not in body and len(part) > BARE_NAME_MAX:
                    continue
                names.add(part)
    except Exception:
        pass
    try:
        from cognition.alias import all_aliases
        for row in all_aliases(memory):
            alias = str(row.get("alias") or "").strip()
            if alias:
                names.add(alias)
    except Exception:
        pass
    return names


def expand_query(query: str, memory) -> str:
    """查询里提到**简称**时，把它的**正名**也拼进查询。

    动机（实测）：三句互补说法里，事实存的是「林小雨」（`林小雨（妹妹）`），
    而用户问的是「小雨是哪个学校的」——**词面对不上就召不回来**。
    别名表知道 `小雨 → 林小雨`，这里把正名补进查询，
    词法（BM25）、二元组、向量三条路就都能匹配上。

    只在真的命中某个简称时才改写查询，所以日常开销近似为零。
    没有别名 / 取不到别名表时**原样返回**——检索绝不能因为这一步而断。
    """
    text = str(query or "")
    if not text:
        return text
    try:
        from cognition.alias import all_aliases
        rows = all_aliases(memory)
    except Exception:
        return text
    extra: list[str] = []
    for row in rows:
        alias = str(row.get("alias") or "").strip()
        canonical = str(row.get("canonical") or "").strip()
        if not alias or not canonical:
            continue
        if alias in text and canonical not in text and canonical not in extra:
            extra.append(canonical)
    if not extra:
        return text
    return text + " " + " ".join(extra)


def lexical_score(query_grams: set[str], text: str,
                  extra_words: set[str] | None = None) -> float:
    if not query_grams or not text:
        return 0.0
    target = keywords(text, extra_words)
    if not target:
        return 0.0
    hit = len(query_grams & target)
    return hit / max(1, len(query_grams))


def _digest(pairs: list) -> str:
    """给内容算一个短指纹（缓存键用）。"""
    import hashlib
    h = hashlib.sha1()
    for item in pairs:
        h.update(repr(item).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _build_bm25(memory: Memory, known: set[str]) -> BM25:
    """建/取 BM25 索引（语义 + 情景一起建，键前缀区分，见 `_lexical_from_bm25`）。

    ⚠ 缓存键**必须含内容指纹**（2026-10-15 修）：
    原实现只用了「条数 + 最大 id」，而文档字符串却写着"内容指纹"——
    于是**两个形状相同的不同库会共用同一份索引**，BM25 分数整个串味。
    实测影响：一个进程里建多个 Companion（探针、测试、会话切换）时，
    后面那个库用的是**第一个库的词法索引**。
    ⚠ 键里也要带 `memory.path`：同一个进程里多个库是很常见的。
    """
    semantic = memory.all_semantic(status="active")
    episodes = memory.recent_episodes(limit=500)
    fingerprint = (
        str(getattr(memory, "path", "")),
        len(semantic),
        max((int(i.get("id") or 0) for i in semantic), default=0),
        len(episodes),
        max((int(i.get("id") or 0) for i in episodes), default=0),
        _digest([(i.get("id"), str(i.get("content") or "")) for i in semantic]
                + [(e.get("id"), str(e.get("summary") or ""),
                    str(e.get("user_text") or "")) for e in episodes]),
        # ⚠ `known` 也必须进键（2026-10-16 修）：它**参与文档分词**
        #   （整词保护靠它），但改它的东西不一定改语义内容——
        #   别名存在**另一张表**（`alias`），登记别名不会动 `semantic` 的摘要。
        #   实测表现：同一个进程里「先检索一次 → 登记别名 → 再检索」，
        #   第二次拿到**别名登记前**缓存的索引 → 别名带来的分数全部失效
        #   → 召不回来，而且换个进程跑又正常（最难查的那种）。
        _digest(sorted(str(x) for x in (known or set()))),
    )
    cached = _BM25_CACHE.get("fp")
    if cached == fingerprint and _BM25_CACHE.get("idx") is not None:
        return _BM25_CACHE["idx"]  # type: ignore[return-value]

    idx = BM25()
    for item in semantic:
        idx.add(f"s{item['id']}", keywords(str(item.get("content") or ""), known))
    for item in episodes:
        text = f"{item.get('summary') or ''} {item.get('user_text') or ''} " \
               f"{item.get('reply_text') or ''}"
        idx.add(f"e{item['id']}", keywords(text, known))
    _BM25_CACHE["fp"] = fingerprint
    _BM25_CACHE["idx"] = idx
    return idx


def reset_bm25_cache() -> None:
    """清 BM25 缓存（改完记忆后强制重建，测试用）。"""
    _BM25_CACHE.clear()


def bm25_scores(query: str, memory: Memory, corpus: str = "semantic",
                known: set[str] | None = None) -> dict[str, float]:
    """取**绝对饱和**后的 BM25 分：doc_id → 0–1。

    ⚠ **这里曾经是 `normalize(raw)`（按本次查询最大值归一），已经改掉**（2026-10-17）。
      归一化的后果很严重：**任何查询里，词法最接近的那一条永远拿到 lex = 1.0**，
      于是 `relevance = max(vec, lex*0.9, blended) ≥ 0.900` ——
      一条**词法只是"最不差"**的干扰句，靠这个虚高的 0.9 把真答案压死。
      实测（37 题标尺）：7/37 的题**榜首是干扰句**，而且相关性都是精确的 0.900
      （不同问题、不同句子都 0.900 —— 一看就知道不是真实分数）。

    改成**绝对饱和** `raw / (raw + K)`：仍然在 0–1、仍能和余弦加权相加
    （原来归一化的目的保住了），但**保留了绝对量级**——
    词法只对上一点点，就只给一点点分。

    K 是半饱和点（raw=K 时 lex=0.5）。K=5 是标尺上扫出来的最优附近；
    K 再大，`raw/(raw+K)` 会低于 `LEXICAL_FLOOR`，退化成"沾边就给 0.3"的平坦保底
    （标尺上 K≥5 结果全一样，说明平坦保底已经够用，但保留 K=5 让它对强命中有区分）。
    """
    idx = _build_bm25(memory, known or set())
    if not len(idx):
        return {}
    query_terms = keywords(query, known)
    if not query_terms:
        return {}
    prefix = "s" if corpus == "semantic" else "e"
    raw = {d: s for d, s in idx.search(query_terms, top_k=500)
           if d.startswith(prefix)}
    return {d: (s / (s + LEX_SATURATION_K) if s > 0 else 0.0)
            for d, s in raw.items()}


def lexical_relevance(query_grams: set[str], doc_id: str,
                      bm25_scored: dict[str, float],
                      legacy: dict[str, float] | None = None) -> float:
    """词法相关度 0–1：**用 BM25 取代旧的「二字组交集比」**。

    旧口径 `lexical_score` 没有 IDF，命中一个「到处都有」的高频字组
    和命中一个只此一家的主题字组得分相同——测试里两者同为 0.333 分不出来
    （`tests/bm25_test.py` 第 6 节）。BM25 用 IDF 把高频字组压下去。

    保底：本查询最高 BM25 分 × LEXICAL_FLOOR。只有一条记忆命中时，
    归一化会给它 1.0（虚高），保底把它压到 0.3，既认它又不让它顶掉向量分。
    `legacy` 是旧口径的分（按 doc_id），取 max 兜底，保证不会比改动前更差。
    """
    score = float(bm25_scored.get(doc_id, 0.0))
    if score > 0:
        score = max(score, LEXICAL_FLOOR)
    if legacy:
        score = max(score, float(legacy.get(doc_id, 0.0)))
    return min(1.0, score)


def relation_targets(query: str, memory: Memory,
                     known: set[str] | None = None) -> list[str]:
    """从查询里的**关系词**出发，找出该跳过去的**名字**（一跳扩展的锚点）。

    锚点形态是 `名字（关系）`（如「林小雨（妹妹）」「工业软件公司（上班的地方）」）：
    查询里出现括号里的关系词 → 取括号外的名字。

    ⚠ **扫全部语义条目，不只 relation 类**（2026-10-15 实测修正）：
    模型对「这条该进 relations 还是 facts」的选择**很不稳定**——
    同一句「我在一家做工业软件的公司上班」，一次进 relations、
    另一次进 facts（`[fact] 对方工业软件公司（上班的地方）`）。
    一跳真正需要的是**那个格式**，不是它落在哪个字段里。
    所以只要条目里有 `名字（关系）` 形态就认，relation 类优先。

    实测依据：只扫 relation 时真实链路多跳 2/3，第 3 条就栽在"锚点进了 facts"。
    """
    out: list[str] = []
    try:
        qgrams = keywords(query, known or set())
        items = list(memory.all_semantic(status="active"))
    except Exception:
        return out
    # relation 类优先：它们本来就是为关系建的，误命中概率更低
    items.sort(key=lambda i: 0 if str(i.get("kind")) == "relation" else 1)
    for item in items:
        body = str(item.get("content") or "")
        for prefix in _HOP_PREFIXES:
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        for m in _HOP_RE.finditer(body):
            name, alias = m.group(1), m.group(2)
            # 查询里出现这个关系词（或它的词元）→ 跳过去
            if alias and (alias in query or alias in qgrams):
                if name and name != alias:
                    out.append(name)
    return list(dict.fromkeys(out))[:HOP_MAX]


def _score_semantic(q: str, memory: Memory, index: VectorIndex | None,
                    known: set[str], bm_scores: dict, rules: dict,
                    exclude_ids: set, include_cold: bool,
                    min_score: float, top_k: int,
                    personal_fallback: bool = False) -> tuple[list[dict], bool]:
    """给一个查询串给语义记忆打分，返回（排好序的 top_k, 是否用到了向量）。

    从 `retrieve()` 里抽出来的——好让**跳数复用同一套打分与闸门逻辑**，
    不另起一套（否则两边的阈值会漂）。`personal_fallback` 只给主查询用：
    跳目标是个名字（"林小雨"），不含"我"，不该触发"补最重要几条"。
    """
    semantic_all = [item for item in memory.all_semantic()
                    if (include_cold or not int(item.get("archived") or 0))
                    and f"semantic#{item['id']}" not in exclude_ids]
    if not semantic_all:
        return [], False

    query_grams = keywords(q, known)
    used_vector = False
    vector_scores: dict[str, float] = {}
    if index is not None and len(index):
        try:
            vector = embed([q])[0]
            for key, score in index.search_vector(vector, top_k=200):
                vector_scores[str(key)] = float(score)
            used_vector = True
        except EmbeddingUnavailable:
            vector_scores = {}

    scored = []
    for item in semantic_all:
        vec = vector_scores.get(f"s{item['id']}", 0.0)
        legacy = lexical_score(query_grams, item["content"])
        lex = lexical_relevance(query_grams, f"s{item['id']}",
                                bm_scores, {f"s{item['id']}": legacy})
        blended = LEXICAL_WEIGHT * lex + (1 - LEXICAL_WEIGHT) * vec
        relevance = max(vec, lex * 0.9, blended)
        if relevance >= min_score or lex >= 0.28 or legacy >= 0.28:
            scored.append((decay_engine.rank_score(item, relevance, rules),
                           relevance, item))
    scored.sort(key=lambda trio: (-trio[0], -trio[1]))
    out = [dict(item, score=round(rank, 3), relevance=round(rel, 3))
           for rank, rel, item in scored[:top_k]]

    # 保底：几乎没有命中、且对方在问自己的事时才补几条重要度最高的。
    # （画像类事实不能因为问法不同就漏掉，但也不能每轮都塞——
    #   实测塞多了模型会把同一条记忆反复搬出来说。）
    if personal_fallback and len(out) < 2 and any(
            mark in (q or "") for mark in ("我", "咱")):
        existing = {item["id"] for item in out}
        for item in sorted(semantic_all,
                           key=lambda it: -decay_engine.decayed_importance(it, rules)):
            if len(out) >= 2:
                break
            if item["id"] in existing:
                continue
            out.append(dict(item, score=0.0, relevance=0.0))
            existing.add(item["id"])
    return out, used_vector


def retrieve(query: str, memory: Memory, index: VectorIndex | None = None,
             top_k_episodes: int = 3, top_k_semantic: int = 4,
             min_score: float = 0.20, recent_limit: int = 3,
             include_cold: bool = False, rules: dict | None = None,
             record_access: bool = True, exclude_ids: set | None = None) -> dict:
    """返回 {semantic, episodes, recent, mode, woke}

    include_cold=True 时冷存储记忆也参与（按需检索），命中后自动唤醒。
    exclude_ids（形如 "episodic#3"）里的条目本轮不再注入——避免同一条记忆刷屏。
    """
    index = index if index is not None else VectorIndex()
    rules = rules or decay_engine.load_rules()
    exclude_ids = exclude_ids or set()
    known = known_names(memory)          # 已认识的专名（人名/宠物名），整词切分
    query = expand_query(query, memory)  # 简称展开：问「小雨」也要搜得到「林小雨」
    query_grams = keywords(query, known)
    mode = "keyword"
    woke: list[str] = []

    # 词法分换成 BM25（带 IDF）。取不到就退回旧口径——检索不能因为这一步而断。
    try:
        bm_semantic = bm25_scores(query, memory, "semantic", known)
        bm_episode = bm25_scores(query, memory, "episode", known)
    except Exception:
        bm_semantic, bm_episode = {}, {}

    # ---- 语义记忆 ----
    semantic, used_vector = _score_semantic(
        query, memory, index, known, bm_semantic, rules, exclude_ids,
        include_cold, min_score, top_k_semantic, personal_fallback=True)
    if used_vector:
        mode = "vector"

    # ---- 一跳扩展：查询里的关系词 → 跳过去再查一轮 ----
    # 实测动机：多跳联想是 0/3，因为查询与答案记忆零词重叠
    # （「我妹妹有什么忌口」↔「林小雨不能吃花生」）。
    # 跳数衰减 γ^t 借自 VCPToolBox 浪潮算法的有限脉冲响应。
    # 只在**真的找到关系锚点**时才多跑一轮，所以日常开销近似为零。
    hop_targets: list[str] = []
    hop_detail: list[dict] = []
    if index is not None or bm_semantic:
        try:
            hop_targets = relation_targets(query, memory, known)
        except Exception:
            hop_targets = []
    if hop_targets:
        merged: dict[int, dict] = {int(it["id"]): dict(it) for it in semantic}
        for t, name in enumerate(hop_targets, 1):
            weight = HOP_DECAY ** t
            # ⚠ 必须**按跳目标重算 BM25**。`_score_semantic` 内部按 name 重新切词，
            # 但如果把主查询的 BM25 分数传进去，词法侧就整体对不上——
            # 实测表现：跳过去了、第二轮却捞不到答案（只有 legacy 兜底偶尔救回一条）。
            try:
                bm_hop = bm25_scores(name, memory, "semantic", known)
            except Exception:
                bm_hop = {}
            sub, sub_vec = _score_semantic(
                name, memory, index, known, bm_hop, rules, exclude_ids,
                include_cold, min_score, top_k_semantic)
            if sub_vec:
                mode = "vector"
            got = []
            for item in sub:
                # 0 分条目不参与（跳过去也可能什么都没命中）
                if float(item.get("score") or 0) <= 0:
                    continue
                got.append(str(item.get("content") or ""))
                hit = dict(item)
                hit["score"] = round(float(item.get("score") or 0) * weight, 3)
                hit["relevance"] = round(float(item.get("relevance") or 0) * weight, 3)
                hit["hop_from"] = name
                hit["hop"] = t
                # 已经命中的不覆盖（主查询的分更可信），只补新的
                merged.setdefault(int(item["id"]), hit)
            hop_detail.append({"target": name, "weight": round(weight, 3),
                               "got": got[:2]})
        # 并集里按衰减后的分数重排，再截断。
        # 注意主查询的 score 是 rank_score（含重要度/新鲜度），
        # 所以这里用同一口径比较，不额外换算。
        #
        # ⚠ **跳进来的条目要保底占位**，不能光按分数截断：
        # 一跳捞到的条目按定义就是「与查询字面重合低」的
        # （妹妹→林小雨→不能吃花生，重合=0），而主查询的高重合命中
        # 分数更高，`[:top_k]` 会把跳进来的一条条挤光——那样跳了等于没跳。
        # 实测：加了 4 条含「妹妹/忌口」的干扰记忆后，跳进来的那条直接不见了。
        # 取舍：普通命中排在**前**面（保证精度不被削弱），跳的占**尾部**名额
        # （衰减后本来就该排后面，但必须进得来）。
        ordered = sorted(merged.values(),
                         key=lambda d: -(float(d.get("score") or 0)))
        hopped = [d for d in ordered if d.get("hop_from")]
        plain = [d for d in ordered if not d.get("hop_from")]
        reserved = hopped[:HOP_MAX]
        semantic = (plain[:max(0, top_k_semantic - len(reserved))] + reserved)[
            :top_k_semantic]

    # ---- 情景记忆 ----
    episodes: list[dict] = []
    if index is not None and len(index):
        try:
            vector = embed([query])[0]
            hits = index.search_vector(vector, top_k=max(top_k_episodes * 6, 20))
            candidates = []
            for key, score in hits:
                if not str(key).startswith("e"):
                    continue
                episode_id = int(str(key)[1:])
                item = memory.episode(episode_id)
                if not item or score < min_score:
                    continue
                if not include_cold and int(item.get("archived") or 0):
                    continue
                if f"episodic#{episode_id}" in exclude_ids:
                    continue
                candidates.append((decay_engine.rank_score(item, float(score), rules),
                                   float(score), item))
            candidates.sort(key=lambda trio: -trio[0])
            episodes = [dict(item, score=round(rank, 3), relevance=round(rel, 3))
                        for rank, rel, item in candidates[:top_k_episodes]]
            if episodes:
                mode = "vector" if mode == "vector" else "mixed"
        except EmbeddingUnavailable:
            episodes = []
    if not episodes:
        # 关键词兜底
        scored = []
        for item in memory.recent_episodes(limit=200):
            if not include_cold and int(item.get("archived") or 0):
                continue
            if f"episodic#{item['id']}" in exclude_ids:
                continue
            text = f"{item['summary']} {item['user_text']} {item['reply_text']}"
            legacy = lexical_score(query_grams, text)
            relevance = lexical_relevance(query_grams, f"e{item['id']}",
                                          bm_episode, {f"e{item['id']}": legacy})
            if relevance > 0.12:
                scored.append((decay_engine.rank_score(item, relevance, rules),
                               relevance, item))
        scored.sort(key=lambda trio: -trio[0])
        episodes = [dict(item, score=round(rank, 3), relevance=round(rel, 3))
                    for rank, rel, item in scored[:top_k_episodes]]

    # 情景记忆也用 BM25 重排（两个分支共用，避免逻辑分叉）。
    # 向量侧对专名/罕见词容易漏，BM25 正好补这一块。
    if episodes and bm_episode:
        rescued = []
        have = {int(it["id"]) for it in episodes}
        for item in memory.recent_episodes(limit=200):
            episode_id = int(item["id"])
            key = f"e{episode_id}"
            score = float(bm_episode.get(key, 0.0))
            if score <= 0 or episode_id in have:
                continue
            if not include_cold and int(item.get("archived") or 0):
                continue
            if f"episodic#{episode_id}" in exclude_ids:
                continue
            # 低于保底分的不捞（只有一条命中时归一化会给 1.0，别被它骗了）
            if score < LEXICAL_FLOOR:
                continue
            rescued.append((decay_engine.rank_score(item, score, rules), score, item))
        if rescued:
            merged = rescued + [(it["score"], it.get("relevance", 0.0), it)
                                for it in episodes]
            merged.sort(key=lambda trio: -trio[0])
            seen: set[int] = set()
            out = []
            for rank, rel, item in merged:
                if int(item["id"]) in seen:
                    continue
                seen.add(int(item["id"]))
                out.append(dict(item, score=round(rank, 3), relevance=round(rel, 3)))
                if len(out) >= top_k_episodes:
                    break
            episodes = out

    # 已经选中的内容（主题与连带召回要去重，别和命中项说同一件事）
    # 同时把**0 分条目剔掉**：关键词完全没命中时它们也会以 0 分留在列表里，
    # 结果被当上下文递给模型（实测递过"喜欢买面包"这种噪音，模型就照着编）。
    semantic = [item for item in semantic if float(item.get("score") or 0) > 0]
    episodes = [item for item in episodes if float(item.get("score") or 0) > 0]
    seen_contents = {str(item.get("content") or item.get("summary") or "")
                     for item in [*semantic, *episodes]}

    # ---- 高层记忆（主题）：与当前输入相关时一起注入（改进 19） ----
    # 注意：主题也要受"最近几轮注入过就不再注入"的约束，否则每轮都塞同一句，
    # 模型会照着那句话反复说（实测出现过连续三轮回复一字不差）。
    try:
        from cognition import themes as themes_engine
        for theme in themes_engine.matches(memory, query, limit=2):
            theme_key = f"theme#{theme.get('id')}"
            if theme_key in exclude_ids or theme["content"] in seen_contents:
                continue
            semantic.append({"id": 0, "kind": "theme", "content": theme["content"],
                             "title": theme["title"], "score": theme.get("score", 0.3),
                             "relevance": 0.0, "theme": True, "key": theme_key,
                             "confidence": 0.6, "evidence_count": theme.get("count", 1),
                             "emotion": "", "archived": 0, "importance": 3.0,
                             "peak": 3.0, "retention": "normal", "created_at": "",
                             "last_access_at": ""})
            seen_contents.add(theme["content"])
            break              # 一轮最多带 1 条高层记忆
    except Exception:
        pass

    # ---- 连带召回（改进 20）：命中项有关联的，把它相关的也带上 ----
    # 同样受排除约束：连着几轮带同一句，等于在教模型复读。
    linked_added = 0
    for table, items, top_k in (("semantic", semantic, top_k_semantic),
                                ("episodic", episodes, top_k_episodes)):
        if len(items) >= top_k or linked_added >= 1:
            continue
        have = {f"{table}#{item['id']}" for item in items}
        for item in list(items):
            if linked_added >= 1:
                break
            key = f"{table}#{item['id']}"
            for linked in memory.linked_entries(key, limit=1):
                if linked.get("key") in have or linked.get("key") in exclude_ids:
                    continue
                text = str(linked.get("summary") or linked.get("content") or "")
                if text and text in seen_contents:
                    continue
                entry = dict(linked)
                entry["score"] = round(float(item.get("score") or 0) * 0.8, 3)
                entry["relevance"] = 0.0
                entry["linked_from"] = key
                if linked.get("key", "").startswith("semantic"):
                    if len(semantic) < top_k:
                        semantic.append(entry)
                        have.add(linked["key"])
                        linked_added += 1
                elif len(episodes) < top_k:
                    episodes.append(entry)
                    have.add(linked["key"])
                    linked_added += 1
                if text:
                    seen_contents.add(text)
                break

    # ---- 被注入的记忆要记一次访问：次数+1、重要度回升、冷存储唤醒 ----
    if record_access:
        for item in semantic:
            updated = memory.touch("semantic", item["id"], "retrieved")
            if updated and updated.get("woke"):
                woke.append(f"语义#{item['id']}")
        for item in episodes:
            updated = memory.touch("episodic", item["id"], "retrieved")
            if updated and updated.get("woke"):
                woke.append(f"情景#{item['id']}")

    return {
        "semantic": semantic,
        "episodes": episodes,
        "recent": memory.recent_episodes(recent_limit),
        "mode": mode,
        "woke": woke,
        # 一跳扩展的轨迹（没跳就是空）。留着便于观测与测试：
        # 一眼看出"这轮为什么多带了一条"以及跳的是哪个关系词。
        "hops": hop_detail,
    }


_KIND_LABEL = {"fact": "对方的事实", "preference": "对方的偏好",
               "relation": "对方提到的人"}


# ---- 逐字原话注入（第③层，2026-10-15）----
# 动机：`retrieve()` 一直返回 `recent`（`episodic.user_text` 的**逐字原话**），
# 但**全仓没人消费 → 从不进提示**。而语义库是**抽取器的渲染版**、会失真
# （实测「林小雨」被抽成「林小记得」），情景摘要也只是 summary 不是原话。
# 所以：**6 轮之外的原话，此前完全没进过提示**，尽管它一直存在库里。
#
# 关键设计：**只注入短期上下文没覆盖的那些**。
# `Generator.history` 已经是最近 `history_turns`（默认 6）轮的逐字原话，
# 再注入同一批就是纯重复、白占预算——所以调用方要把 history 里的文本传进来排除。
VERBATIM_MAX_ITEMS = 2        # 最多引两句，别把块撑长（块越长模型越容易复述提示词）
VERBATIM_ITEM_CHARS = 40      # 每句截断长度
VERBATIM_RESERVE_MAX = 110    # 给原话预留的上限（还要受 max_chars 约束）


def _verbatim_lines(result: dict, exclude: set[str] | None = None,
                    limit: int = VERBATIM_MAX_ITEMS) -> list[str]:
    """挑出「较早的、短期上下文没覆盖的」原话。

    `exclude` 是当前 history 里已有的文本（含 user 与 assistant），
    命中就跳过——那部分模型已经能直接看到。

    `COMPANION_NO_VERBATIM=1` 可关掉整层（A/B 对照与排查用）。
    """
    if os.environ.get("COMPANION_NO_VERBATIM") == "1":
        return []
    exclude = {str(x).strip() for x in (exclude or set()) if str(x).strip()}
    out: list[str] = []
    seen: set[str] = set()
    for item in result.get("recent") or []:
        text = str(item.get("user_text") or "").strip()
        if len(text) < 4 or text in seen or text in exclude:
            continue
        # 摘要与原话都进了提示就会重复；只留原话
        seen.add(text)
        quote = text[:VERBATIM_ITEM_CHARS]
        out.append(f"“{quote}”")
        if len(out) >= limit:
            break
    return out


def build_memory_block(result: dict, max_chars: int | None = None,
                       exclude_texts: set[str] | None = None) -> str:
    """把检索结果压成一小段可注入系统提示的记忆文本。

    三点讲究：
    1. 刻意保持很短——模型对系统提示长度敏感，记忆块越长越容易复述提示词；
    2. 必须写明"这些说的是对方、不是你"——否则模型会把对方的事实说成自己的
      （实测出现过"我 usually 凌晨两点睡"这种把用户作息当成自己作息的回答）；
    3. **逐字原话单独一行、且只收 short-term 窗口之外的**（见 `_verbatim_lines`）：
      语义库是抽取器的渲染版会失真，原话是"料"的兜底；但最近几轮的原话
      已经在 `Generator.history` 里了，重复注入纯属浪费预算。

    长度上限默认取 config/decay.yaml 的 max_inject_chars（上下文预算，改进 10）。
    """
    if max_chars is None:
        max_chars = int(decay_engine.load_rules().get("max_inject_chars", 380))
    lines: list[str] = []
    seen: set[str] = set()

    # 先算原话：它要有**保底名额**，不能被后面的截断砍掉（那正是它存在的意义）。
    verbatim = _verbatim_lines(result, exclude_texts)
    reserve = min(VERBATIM_RESERVE_MAX, max_chars // 3) if verbatim else 0
    facts_budget = max_chars - reserve

    by_kind: dict[str, list[str]] = {}
    for item in result.get("semantic", []):
        if item.get("theme"):
            continue                     # 高层记忆单独一行，别混进「事实/偏好」
        content = str(item["content"]).strip()
        # 记忆里存的是规范句（带主体"对方…"），但这里的标签已经写了"对方的偏好"，
        # 渲染时剥掉行首主体，免得出现"对方的偏好：对方学习吉他"这种重复
        label = _KIND_LABEL.get(item.get("kind"), "")
        if label.startswith("对方"):
            for prefix in ("对方提到的人：", "对方"):
                if content.startswith(prefix):
                    content = content[len(prefix):]
                    break
        if len(content) < 2 or content in seen:
            continue
        seen.add(content)
        by_kind.setdefault(item["kind"], []).append(content)
    for kind in ("fact", "preference", "relation"):
        contents = by_kind.get(kind)
        if contents:
            lines.append(f"- {_KIND_LABEL[kind]}：" + "；".join(contents))
    theme_lines = [str(item.get("title") or item.get("content") or "")[:24]
                   for item in result.get("semantic", []) if item.get("theme")]
    if theme_lines:
        lines.append("- 长期印象：" + "；".join(theme_lines))

    # 逐字原话放**摘要之前**：它是"料"的兜底，比摘要更该保住。
    if verbatim:
        lines.append("- 对方原话：" + "；".join(verbatim))

    # 情景摘要放最后，所以每条都压短，避免被整体截断时丢掉（曾因此答不出"下个月有什么大事"）
    summaries = [str(item["summary"])[:24] for item in result.get("episodes", [])
                 if item.get("summary")][:2]
    if summaries:
        lines.append("- 你们聊过：" + "；".join(summaries))

    if not lines:
        return ""

    header = ("（以下是关于对方的记忆：说的都是对方，不是你自己。回答时把对方称作「你」，"
              "不要用「我」来说对方的事。只在对方问起或确实相关时才提，"
              "不要每句都拿这些出来说。不要生硬复述，也不要说「根据我的记忆」）")
    text = header + "\n" + "\n".join(lines)
    # ⚠ 截断要**扣除原话那一行的保底**：直接切尾巴会把它砍掉，
    #   而它存在的意义正是"事实那条不可靠时还有原话"。
    if len(text) > max_chars:
        if verbatim and reserve:
            keep = max(facts_budget, max_chars - reserve)
            text = text[:keep].rstrip("；-，, ") + "…"
        else:
            text = text[:max_chars].rstrip("；-，, ") + "…"
    return text

