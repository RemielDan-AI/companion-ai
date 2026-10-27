"""别名表：把「小雨」和「林小雨」认成同一个人。

## 为什么单独存表

`林小雨（妹妹）` 这种形态只能让「妹妹」跳到「林小雨」，
而用户换个说法叫「小雨」时，系统就认不出来了
（实测：三句互补说法里，第 3 句「小雨是广州大学的学生」
问「小雨是哪个学校的」完全召不回来）。

**为什么不每次现推**：
  · 别名推错是**正确性 bug**——会把两个人的事混到一起，比搜不到严重得多。
    存表才能审计、才能人工否掉、才能追问"这条是谁学的"。
  · 启发式规则每次现推**不稳定**（同一问题两次结果可能不同）；存下来就固定了。
  · 只有本人知道的事（「小雨就是林小雨」）规则永远推不出来，要能手工加。

## 护栏（别名错了比没有更糟）

    ① alias 必须**短于** canonical（简称不能比全名长）
    ② alias 不能是**泛称/类型词**（公司、大学、妹妹、同事…）
    ③ **一个 alias 只能指向一个 canonical**：已有不同指向时**不再插入**，
       记一条日志等人工裁决（与冲突处理同一个哲学：不猜、不覆盖）
    ④ **不许成环**（A→B 且 B→A）
    ⑤ `status='rejected'` 的永不复活（人工否掉要算数）

## 别名从哪来

    `suffix`   简称规则：人名「林小雨」的末 2 字「小雨」，
               且**用户原话里真的出现过**「小雨」（这是关键证据，不然纯猜）
    `user`     用户手填
    `import`   导入
（关系词如「妹妹」不走这张表——`relation_targets()` 已经用 `名字（关系）` 那一跳覆盖了，
  再走一遍会变成两套机制互相打架。）
"""
from __future__ import annotations

from cognition.terms import NAME_MIN, PAIR_RE, is_name_like

# 泛称/类型词：这些是"某类东西"，不是"某个人"，不能当别名。
# ⚠ 这份表是防"简称规则"误伤的：例如「王先生」的末 2 字是「先生」、
#   「李老师」的末 2 字是「老师」——它们会出现在原话里，但显然不是别名。
_GENERIC = frozenset(
    "公司 大学 学校 学院 医院 银行 工厂 单位 部门 集团 中心 基地 研究所 "
    "先生 女士 小姐 老师 医生 护士 律师 经理 老板 师傅 大夫 教授 "
    "朋友 同事 同学 室友 邻居 家人 父母 爸爸 妈妈 父亲 母亲 哥哥 姐姐 "
    "弟弟 妹妹 儿子 女儿 孩子 女儿 老婆 老公 对象 男友 女友 亲戚 "
    "宠物 猫 狗 鸟 兔 仓鼠 乌龟 鱼 "
    "地方 城市 老家 地址 住所 学校 上班 工作 职业 学生 家长 "
    "学生 小孩 宝宝 主人 客人 领导 下属 同事 前辈 后辈".split()
)

# 简称可能取的长度：中文名全名多为 3 字（姓+名），简称取末 2 字；
# 4 字名（复姓或双名）简称可能是末 2 或末 3 字。
_SUFFIX_LENGTHS = (2, 3)

# 身份类型：**不同类型不能互为别名**。
# 实测踩过（2026-10-16）：提示只给一个选项时，2B 会把「团子」（猫）
# 指认成「林小雨」（妹妹）——**诱导性提问**，模型没得选就挑它。
# 除了改提示（把 `名字（关系）` 一起给它），程序这边也必须**硬拦**：
# 猫和人不可能是同一个，这条不该靠模型自觉。
_TYPE_WORDS = {
    "person": ("妹妹", "姐姐", "哥哥", "弟弟", "妈妈", "爸爸", "母亲", "父亲",
               "同事", "朋友", "同学", "室友", "邻居", "男友", "女友", "对象",
               "老婆", "老公", "儿子", "女儿", "亲戚", "家人", "外甥", "侄女",
               "侄子", "叔叔", "阿姨", "舅舅", "姑姑", "爷爷", "奶奶", "姥姥",
               "姥爷", "老师", "医生", "学生", "本人", "用户提到的家人"),
    "pet": ("宠物", "猫", "狗", "鸟", "兔", "仓鼠", "乌龟", "鱼", "毛孩子"),
    "org": ("公司", "单位", "学校", "大学", "学院", "医院", "银行", "机构",
            "研究所", "集团", "工厂", "部门", "团队", "组织", "上班的地方"),
    "place": ("地方", "地点", "城市", "老家", "地址", "住所", "住处", "城市",
              "国家", "省份", "上学地点"),
}


def rel_type(word: str) -> str:
    """把关系词归成身份类型；认不出就返回空串（表示"不确定"）。"""
    text = str(word or "")
    for kind, words in _TYPE_WORDS.items():
        if any(w in text for w in words):
            return kind
    return ""


def compatible(kind_a: str, kind_b: str) -> bool:
    """两种身份能不能互为别名。**认不出就不拦**（宁可放过也不误拦）。"""
    if not kind_a or not kind_b:
        return True
    return kind_a == kind_b

# 语义记忆里挂在正文前面的主体前缀。收集"已知全名"时要先剥掉，
# 否则正名会带上前缀（实测踩过：`小雨 → 对方提到的人：林小雨`）。
# 与 `engine/retrieve._HOP_PREFIXES` 保持一致；本地再写一份是为了避免循环导入
# （retrieve 已经 import memory）。
_CONTENT_PREFIXES = ("对方提到的人：", "对方", "用户")


def is_generic(text: str) -> bool:
    return str(text or "").strip() in _GENERIC


def _rows(memory, **where) -> list[dict]:
    sql = "SELECT * FROM alias WHERE status='active'"
    args: list = []
    for key, value in where.items():
        sql += f" AND {key}=?"
        args.append(value)
    sql += " ORDER BY confidence DESC, id DESC"
    return [dict(r) for r in memory.conn.execute(sql, args).fetchall()]


def all_aliases(memory, status: str = "active") -> list[dict]:
    """全部别名行（便于观测/审计/GUI 展示）。"""
    return [dict(r) for r in memory.conn.execute(
        "SELECT * FROM alias WHERE status=? ORDER BY canonical, alias",
        (status,)).fetchall()]


def aliases_of(memory, canonical: str) -> set[str]:
    """这个名字有哪些简称。"""
    return {r["alias"] for r in _rows(memory, canonical=canonical)}


def canonical_of(memory, name: str) -> str:
    """这个简称的正名（没有就返回它自己）。多指向时返回**最长**的那个。"""
    rows = _rows(memory, alias=name)
    if not rows:
        return name
    return max((r["canonical"] for r in rows), key=len)


def expand(memory, names) -> set[str]:
    """把一组名字补全成「原样 + 各自的正名 + 各自的简称」。

    检索时用：查询里出现「小雨」→ 也把「林小雨」加进去搜，
    否则存的是「林小雨」、问的是「小雨」，词面对不上就召不回来。
    """
    out: set[str] = set()
    for name in names or []:
        name = str(name or "").strip()
        if not name:
            continue
        out.add(name)
        out.add(canonical_of(memory, name))
        out |= aliases_of(memory, name)
        out |= aliases_of(memory, canonical_of(memory, name))
    return {n for n in out if n}


def add_alias(memory, alias: str, canonical: str, kind: str = "",
              source: str = "suffix", confidence: float = 0.5,
              episodic_id: int = 0, note: str = "") -> dict:
    """登记一条别名。返回 {"action", "reason", "alias", "canonical"}。

    action: `added` / `exists`（已有一条一样的）/ `rejected`（护栏或冲突）
    """
    from cognition.memory import now

    alias = str(alias or "").strip()
    canonical = str(canonical or "").strip()
    if not alias or not canonical:
        return {"action": "rejected", "reason": "空", "alias": alias,
                "canonical": canonical}
    if alias == canonical:
        return {"action": "rejected", "reason": "和正名相同", "alias": alias,
                "canonical": canonical}
    if len(alias) < NAME_MIN:
        return {"action": "rejected", "reason": f"简称太短（<{NAME_MIN}）",
                "alias": alias, "canonical": canonical}
    if len(alias) >= len(canonical):
        return {"action": "rejected", "reason": "简称不比正名短", "alias": alias,
                "canonical": canonical}
    if is_generic(alias):
        return {"action": "rejected", "reason": f"「{alias}」是泛称/类型词",
                "alias": alias, "canonical": canonical}
    # 已有人工否掉的记录 → 不再复活
    dead = memory.conn.execute(
        "SELECT 1 FROM alias WHERE alias=? AND canonical=? AND status='rejected'",
        (alias, canonical)).fetchone()
    if dead:
        return {"action": "rejected", "reason": "这条以前被否掉过",
                "alias": alias, "canonical": canonical}

    existing = [dict(r) for r in memory.conn.execute(
        "SELECT * FROM alias WHERE alias=?", (alias,)).fetchall()]
    if any(r["canonical"] == canonical and r["status"] == "active"
           for r in existing):
        return {"action": "exists", "reason": "已有同样的别名",
                "alias": alias, "canonical": canonical}
    # ⚠ ③ 一个简称只能指一个人：已有**别的**指向就绝不覆盖，记日志等人工
    others = [r["canonical"] for r in existing if r["status"] == "active"]
    if others:
        memory.log("alias_conflict", "alias", 0,
                   f"「{alias}」已被认作 {others}，本条想指向「{canonical}」——"
                   f"不覆盖，留待人工裁决")
        return {"action": "rejected",
                "reason": f"「{alias}」已有别的指向 {others}",
                "alias": alias, "canonical": canonical}
    # ⚠ ④ 不许成环
    if canonical_of(memory, canonical) in (alias, canonical) and \
            canonical_of(memory, canonical) == alias:
        return {"action": "rejected", "reason": "会成环", "alias": alias,
                "canonical": canonical}

    stamp = now()
    memory.conn.execute(
        "INSERT INTO alias (created_at, updated_at, alias, canonical, kind,"
        " source, confidence, status, episodic_id, note)"
        " VALUES (?,?,?,?,?,?,?,'active',?,?)",
        (stamp, stamp, alias, canonical, kind, source, float(confidence),
         int(episodic_id or 0), note))
    memory.conn.commit()
    memory.log("alias", "alias", 0,
               f"学到别名「{alias}」→「{canonical}」（{source}，"
               f"置信 {confidence}）")
    return {"action": "added", "reason": "", "alias": alias,
            "canonical": canonical}


def reject_alias(memory, alias: str, canonical: str, note: str = "") -> dict:
    """人工否掉一条别名（保留痕迹，不删）。"""
    from cognition.memory import now
    memory.conn.execute(
        "UPDATE alias SET status='rejected', updated_at=?, note=? WHERE alias=?",
        (now(), note or "人工否掉", alias))
    memory.conn.commit()
    memory.log("alias_reject", "alias", 0, f"否掉别名「{alias}」（{note}）")
    return {"action": "rejected", "alias": alias, "canonical": canonical}


def full_names(memory) -> set[str]:
    """从语义库里收集"看起来是专名"的**全名**（正名候选）。

    与 `engine/retrieve.known_names` 同源，但这里**不收别名**——
    本函数是给"学别名"用的，收了别名就会自己指自己。

    ⚠ **必须过 `is_name_like`**（2026-10-16 踩过）：
      第一版只要长度 ≥3 就收，于是**整句**「妹妹在广州上学」被当成名字，
      末 2 字「上学」又被当成它的简称 → 别名表里混进
      `上学 → 妹妹在广州上学` 这种垃圾，而 `known_names()` 又会把它
      当已知专名整词挖掉，反过来破坏分词。
      `is_name_like` 恰好能挡：整句含虚词（在/是/叫…）或以主体开头。
    """
    out: set[str] = set()
    try:
        items = memory.all_semantic(status="active")
    except Exception:
        return out
    for item in items:
        body = str(item.get("content") or "")
        for prefix in _CONTENT_PREFIXES:
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        for part in body.replace("，", "（").split("（"):
            part = part.strip("） 　(")
            if len(part) >= 3 and is_name_like(part):
                out.add(part)
    return out


def learn_from_turn(memory, user_text: str, relations, episodic_id: int = 0,
                    known: set[str] | None = None) -> list[dict]:
    """本轮学别名：拿已知的**全名**，去用户原话里找它的**简称**。

    对每个全名 N（长度 ≥ 3），取末 2 / 末 3 字当候选；
    **候选必须真的出现在用户原话里**才算证据——这是本规则的关键，
    否则就是纯猜（「林小雨」的末 2 字是「小雨」，但用户从没这么叫过就不该记）。

    返回本轮新增/被拒的记录，便于观测与测试。
    """
    text = str(user_text or "")
    out: list[dict] = []
    names = {str(n or "").strip() for n in (relations or []) if str(n or "").strip()}
    names |= {str(n or "").strip() for n in (known or set())}
    if known is None:
        names |= full_names(memory)      # 没传就自己收（调用方少踩一个坑）
    for name in sorted(names):
        if len(name) < 3:
            continue
        # ⚠ 必须**先把全名从原话里挖掉**再找候选（2026-10-16 踩过）：
        #   候选天然是全名的**子串**（「州大学」⊂「广州大学」），
        #   所以只要全名出现在原话里，"候选也出现在原话里"就被**平凡满足**了
        #   → 别名表混进 `州大学 → 广州大学` 这种垃圾。
        #   挖掉之后，候选必须**独立出现**才算证据。
        rest = text.replace(name, "")
        for k in _SUFFIX_LENGTHS:
            if k >= len(name):
                continue
            cand = name[-k:]
            if len(cand) < NAME_MIN or cand not in rest:
                continue          # ← 证据：用户**独立**这么叫过
            got = add_alias(memory, cand, name, source="suffix",
                            confidence=0.6, episodic_id=episodic_id,
                            note="简称规则（原话里独立出现过）")
            if got["action"] == "added":
                out.append(got)
    return out


def backfill(memory, limit: int = 500) -> dict:
    """回扫历史原话补学别名——处理「**简称先到、全名后到**」。

    场景：用户先说「小雨是广州大学的学生」（此时还不知道全名），
    过几天才说「我妹妹叫林小雨」。入库那一刻学不到，得回头补。

    与索引回填同一个思路：拿现在的已知全名，去历史原话里找证据。
    """
    known = full_names(memory)
    added: list[dict] = []
    try:
        episodes = memory.recent_episodes(limit=limit)
    except Exception:
        episodes = []
    for ep in episodes:
        added += learn_from_turn(memory, ep.get("user_text") or "",
                                 relations=[], episodic_id=int(ep.get("id") or 0),
                                 known=known)
    return {"action": "done" if added else "nothing",
            "added": len(added), "aliases": added}


# ---- 模型提议、程序裁决（2026-10-16）----
# 规则只能抓**后缀简称**（林小雨→小雨）；换个叫法就抓不到了
# （阿雨、小林子、老林…）。这类要靠理解上下文：模型知道
# 「小雨是广州大学的学生」里的「小雨」就是前文那个「林小雨」。
#
# ⚠ 关键设计：**让模型做闭集选择**——它只能从「已知的人」里挑一个，
#   或者回答「新」。这样它**没有机会编造一个新映射**（输出空间被程序限死），
#   而 2B 做「从 3 个选项里选一个」比"自由生成一个别名"可靠得多。
#   **写入与否仍由程序裁决**：模型只是提议，`add_alias` 的护栏一条都不少。
ALIAS_SYSTEM = "你在判断两个称呼是不是同一个人。只输出答案，不要解释。"

ALIAS_TEMPLATE = """已知的人：
{known}

用户说：{text}

其中的「{cand}」是不是上面某个人？是就只回答那个人的名字；
**不是同一个人**（是别的人、宠物、单位）就只回答：新"""


def _relation_of(memory, name: str) -> str:
    """查这个名字在库里记的关系词（`名字（关系）` 的括号部分）。"""
    try:
        items = memory.all_semantic(status="active")
    except Exception:
        return ""
    for item in items:
        body = str(item.get("content") or "")
        for prefix in _CONTENT_PREFIXES:
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        for m in PAIR_RE.finditer(body):
            if m.group(1) == name:
                return str(m.group(2) or "")
    return ""


def _pick_from_known(answer: str, known) -> str:
    """把模型的回答**收成闭集里的一个**：不在已知名单里就当它没回答。

    这是整套护栏里最关键的一步——模型输出再离谱，也只能落到
    「已知全名之一」或「什么都没有」。**精确等优先**，再做包含匹配
    （防模型回「是林小雨」这种带前缀的写法）。
    """
    text = str(answer or "").strip().strip("“”\"'。. ")
    if not text:
        return ""
    for name in sorted(known, key=len, reverse=True):
        if text == name:
            return name
    for name in sorted(known, key=len, reverse=True):
        if name in text:
            return name
    return ""


def unknown_candidates(relations, known) -> list[str]:
    """本轮关系里**还没被认识**的名字（候选：可能是某人的另一种叫法）。"""
    out: list[str] = []
    for raw in (relations or []):
        text = str(raw or "")
        for part in text.replace("，", "（").split("（"):
            part = part.strip("） 　(")
            for prefix in _CONTENT_PREFIXES:
                if part.startswith(prefix):
                    part = part[len(prefix):]
                    break
            if (len(part) >= NAME_MIN and is_name_like(part)
                    and not is_generic(part)          # 角色词/类型词不配当候选
                    and part not in known and part not in out):
                out.append(part)
    return out


def model_propose_aliases(memory, generator, user_text: str, relations,
                          episodic_id: int = 0, limit: int = 3) -> list[dict]:
    """让模型给"本轮新出现的名字"指认一个已知人物；**由程序决定写不写**。

    返回每次尝试的记录（含被拒原因），便于观测与测试。
    """
    import os
    out: list[dict] = []
    if os.environ.get("COMPANION_NO_ALIAS_MODEL") == "1":
        return out
    if generator is None:
        return out
    known = full_names(memory)
    if len(known) < 1:
        return out
    cands = unknown_candidates(relations, known)[:limit]
    if not cands:
        return out
    # 提示里带上**身份**（`林小雨（妹妹）`），否则模型没依据判断"是不是同一个人"
    # ——实测只给名字时它会把猫指认成人。
    ordered = sorted(known, key=len, reverse=True)[:8]
    known_list = "\n".join(
        f"- {n}（{_relation_of(memory, n) or '身份不明'}）" for n in ordered)
    # 候选自己的身份（从本轮关系里取），用于**类型硬拦**
    cand_types = {}
    for raw in (relations or []):
        for m in PAIR_RE.finditer(str(raw or "")):
            cand_types[m.group(1)] = str(m.group(2) or "")
    for cand in cands:
        cand_kind = rel_type(cand_types.get(cand, ""))
        try:
            answer = generator.complete(
                ALIAS_SYSTEM,
                ALIAS_TEMPLATE.format(known=known_list, text=str(user_text)[:80],
                                      cand=cand),
                max_tokens=24, temperature=0.1)
        except Exception as exc:
            out.append({"alias": cand, "canonical": "", "action": "unresolved",
                        "reason": f"模型不可用：{type(exc).__name__}"})
            continue
        canonical = _pick_from_known(answer, known)
        if not canonical:
            out.append({"alias": cand, "canonical": "", "action": "unresolved",
                        "reason": f"模型没指向已知的人（回答：{str(answer)[:20]!r}）"})
            continue
        # ⚠ 类型硬拦：猫和人不能互为别名。不该靠模型自觉。
        canon_kind = rel_type(_relation_of(memory, canonical))
        if not compatible(cand_kind, canon_kind):
            memory.log("alias_reject", "alias", 0,
                       f"类型不符：「{cand}」是{canon_kind and cand_kind}"
                       f"、「{canonical}」是{canon_kind}——不写")
            out.append({"alias": cand, "canonical": canonical,
                        "action": "rejected",
                        "reason": f"类型不符（候选={cand_kind or '?'}，"
                                  f"正名={canon_kind or '?'}）"})
            continue
        got = add_alias(memory, cand, canonical, kind=cand_kind, source="model",
                        confidence=0.55, episodic_id=episodic_id,
                        note=f"模型指认（原话：{str(user_text)[:24]}）")
        got["answer"] = str(answer)[:24]
        out.append(got)
    return out
