"""认知提取：每轮对话后生成摘要/情绪/重要度，抽取事实/偏好/关系，并更新个人画像。

阶段 2 新增。模型调用走 engine.generate.Generator.complete（单轮、低温、无历史）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re

from cognition.memory import Memory
from cognition.terms import find_called_names
from engine import emotion as emotion_mod

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "cognition" / "profile.json"

EXTRACT_SYSTEM = (
    "你是记忆整理器。只输出一个 JSON 对象，不要解释，不要代码块，不要多余文字。"
)

EXTRACT_TEMPLATE = """用户说：{user}

未央回答：{reply}

整理这一轮，只输出一个 JSON 对象：
{{"summary": "一句话概括，不超过30字",
 "emotion": "从 平静/开心/低落/疲惫/焦虑/生气/孤独/期待/压力 里选一个",
 "importance": 1到5的整数，越关系到长期的事越高,
 "facts": ["关于用户和用户提到的家人、宠物、单位的具体事实：职业、家人、宠物、身体状况、重要日期、计划、行程、约定；宠物的具体状态（吃什么、多大、什么病）；住所或单位的变动"],
 "preferences": ["偏好或习惯，如口味、作息、喜欢或讨厌的事"],
 "relations": ["提到的人、宠物、机构、地点，每条写成「名字（关系）」，如 林小雨（妹妹）、团子（宠物）、工业软件公司（上班的地方）"]}}

规则：
- 只写用户说过的内容，不推测、不编造，也不要把"用户"本身当成一条。
- relations 每条都写成「名字（关系）」：括号外是具体名字，括号里是用户怎么称呼它。
  人名、宠物、单位、常去的地方都算，不要只收人名。只有关系词、说不出名字时就只写关系词。
  只收**具体实体名**；"后端""职业"这类泛称不收（实测压缩提示后会漏出这种）。
- facts 只收有具体内容的条目（名字、数字、具体名词、具体去处）。
  "有点累""压力大"这类纯状态不要写进 facts —— 那是情绪，已由 summary/emotion 记。
- facts 要保留主体：用户说"我公司下个月要搬去滨江"就写"用户公司下个月搬去滨江"，
  不要写成"用户搬去滨江"（那是把公司的事记成了用户的事）。
- 没有内容的字段给空数组。
重要度参考：日常闲聊 1-2，情绪与生活状态 3，长期计划或重要关系 4-5。

示例：
用户说：我妹妹叫林小雨，她在一家做工业软件的公司上班。
输出：{{"summary": "用户妹妹林小雨在工业软件公司上班", "emotion": "平静", "importance": 4,
 "facts": ["用户妹妹林小雨在工业软件公司上班"], "preferences": [],
 "relations": ["林小雨（妹妹）", "工业软件公司（上班的地方）"]}}"""

# ⚠ 下面这些是**给人看的踩坑记录**，故意留在注释里、**不写进提示词**。
#   提示词越短 prompt-eval 越快（实测 985 token 的提示在 CPU 上要 21 秒，
#   而抽取是**每轮**都要跑的）。2026-10-15 把 rationale 从提示词搬到这里，
#   提示词从 1687 字压到约 800 字。

# ============================================================================
# **拆分策略的两个提示词**（2026-10-17，默认策略）
# ============================================================================
# 为什么不整套跑两遍、而是**拆成两半各跑一次**（同为 2 次调用）——
# 三次独立复现的受控对比（`tests/analysis/split_vs_union_probe.py`，
# n=120）：
#
#        方法            主体        值        字段     编造
#        ① 只拆两次      120/120   120/120   106/120    10
#        ② 只并集两次    113/120   116/120   101/120     0
#        ③ 拆×并(4次)    120/120   120/120    93/120    16
#
# 过闸门之后（`split_guard_probe.py`，n=96）：
#
#        ① 只拆两次    主体 96/96   值 95/96   每条挡 0.18
#        ② 只并集两次  主体 92/96   值 87/96   每条挡 0.15
#
# **决定性的是「值」（丢宾语）——① 比 ② 多 8 个样本**，
# 而"丢宾语"本来就被认定是抽取器最主要的毛病。
#
# ⚠ ① 的原始编造确实更多（10 vs 0），但**过闸门后只差 0.03**——
#   脏的那部分被 `_grounded`/`_absolute_ok`/`_subject_ok` 挡住了。
#
# ⚠ 代价：**容错变差**。每个半场只有一次调用，JSON 解析失败就整个半场丢失
#   （并集策略的两次互为备份）。所以这里配了 `_complete_json(..., retries=1)`。
#
# ⚠ 另一处损失：**没有 `agreement` 信号了**（每个字段只有一份样本）。
#   `agreement` 是唯一可靠的置信度信号（模型自报那个实测 84% 都 ≥0.9、
#   不区分好坏），组装层现在用"接地强度+证据数"顶上。
TEMPLATE_FACTS = """用户说：{user}

未央回答：{reply}

只整理**事实和关系**，输出一个 JSON 对象，只要这两个字段：
{{"facts": ["关于用户和用户提到的家人、宠物、单位的具体事实：职业、家人、宠物、身体状况、重要日期、计划、行程、约定；宠物的具体状态（吃什么、多大、什么病）；住所或单位的变动"],
 "relations": ["提到的人、宠物、机构、地点，每条写成「名字（关系）」，如 林小雨（妹妹）、团子（宠物）、工业软件公司（上班的地方）"]}}

规则：
- 只写用户说过的内容，不推测、不编造，也不要把"用户"本身当成一条。
- relations 每条都写成「名字（关系）」：括号外是具体名字，括号里是用户怎么称呼它。
  人名、宠物、单位、常去的地方都算，不要只收人名。只有关系词、说不出名字时就只写关系词。
  只收**具体实体名**；"后端""职业"这类泛称不收。
- facts 只收有具体内容的条目（名字、数字、具体名词、具体去处）。
  "有点累""压力大"这类纯状态不要写进 facts。
- facts 要保留主体：用户说"我公司下个月要搬去滨江"就写"用户公司下个月搬去滨江"，
  不要写成"用户搬去滨江"（那是把公司的事记成了用户的事）。
- 没有内容的字段给空数组。

示例：
用户说：我妹妹叫林小雨，她在一家做工业软件的公司上班。
输出：{{"facts": ["用户妹妹林小雨在工业软件公司上班"],
 "relations": ["林小雨（妹妹）", "工业软件公司（上班的地方）"]}}"""

TEMPLATE_META = """用户说：{user}

未央回答：{reply}

只做**概括和情绪**，输出一个 JSON 对象，只要这四个字段：
{{"summary": "一句话概括，不超过30字",
 "emotion": "从 平静/开心/低落/疲惫/焦虑/生气/孤独/期待/压力 里选一个",
 "importance": 1到5的整数，越关系到长期的事越高,
 "preferences": ["偏好或习惯，如口味、作息、喜欢或讨厌的事"]}}

规则：
- 只写用户说过的内容，不推测、不编造。
- preferences 只收偏好与习惯；纯状态（"有点累"）不算偏好。
- 没有内容的字段给空数组。

示例：
用户说：我妹妹叫林小雨，她在一家做工业软件的公司上班。
输出：{{"summary": "用户妹妹林小雨在工业软件公司上班", "emotion": "平静",
 "importance": 4, "preferences": []}}"""


def _complete_json(generator, template: str, user_text: str, reply_text: str,
                   retries: int = 1, max_tokens: int = 400) -> dict:
    """发一次结构化抽取；**解析失败就重试**（拆分策略的容错保险）。

    ⚠ 拆分后每个半场只有一次调用，失败了那半场就整个丢——
      这和并集策略"两次互为备份"不一样，所以必须自己补重试。
    """
    prompt = template.format(user=user_text, reply=reply_text)
    for attempt in range(max(1, retries + 1)):
        raw = ""
        try:
            raw = generator.complete(EXTRACT_SYSTEM, prompt,
                                     max_tokens=max_tokens, temperature=0.2)
        except Exception:
            raw = ""
        data = parse_json(raw)
        if data:
            return data
        if attempt == 0 and retries:
            continue
    return {}


def extract_turn_split(generator, user_text: str, reply_text: str) -> dict:
    """**拆分策略**：事实/关系 一次调用，概括/情绪/偏好 另一次调用。

    见 `TEMPLATE_FACTS` / `TEMPLATE_META` 上方的对比数据。
    """
    facts_data = _complete_json(generator, TEMPLATE_FACTS, user_text, reply_text)
    meta_data = _complete_json(generator, TEMPLATE_META, user_text, reply_text)
    return {
        "summary": (str(meta_data.get("summary") or "").strip())[:60]
                   or user_text.strip()[:30],
        "emotion": (emotion_mod.normalize(meta_data.get("emotion"))
                    if meta_data.get("emotion") else emotion_mod.guess(user_text)),
        "importance": _clamp_importance(meta_data.get("importance", 3)),
        "facts": _as_list(facts_data.get("facts")),
        "preferences": _as_list(meta_data.get("preferences")),
        "relations": _as_list(facts_data.get("relations")),
        "raw_ok": bool(facts_data or meta_data),
        "strategy": "split",
        "agreement": {},
    }
#
#   · facts 的措辞踩过：原来写"关于**用户自己**的稳定信息"，结果
#     「林小雨不能吃花生」因为主体不是用户被整句丢掉 →
#     「我妹妹有什么忌口」永远答不出来（多跳链在数据层就断）。所以现在是
#     "关于用户**和用户提到的家人、宠物、单位**"。
#   · relations 的措辞踩过：原来只有人名例子（妹妹、同事小李、妈妈），
#     模型就只收人名；现在给了人/宠物/机构三类例子。
#   · 提示词里**绝对不要用 markdown 加粗**（`**词**`）：实测模型会把星号
#     当内容抄进记忆（存出过「用户**公司**下个月搬去滨江」）。要强调就用中文引号。
#   · 示例只留**一个**：两个完整示例多花约 300 字，而收益主要在"名字（关系）"
#     这一个形态上，一个把人和机构都覆盖到的例子就够。


def parse_json(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象（模型常会带前后废话）。"""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = cleaned[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        # 常见毛病：尾随逗号、中文引号
        fixed = re.sub(r",\s*([}\]])", r"\1", candidate).replace("“", '"').replace("”", '"')
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _clamp_importance(value) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, number))


def extract_turn(generator, user_text: str, reply_text: str) -> dict:
    """让模型整理本轮。失败时退回关键词兜底，保证流程不中断。"""
    prompt = EXTRACT_TEMPLATE.format(user=user_text, reply=reply_text)
    raw = ""
    try:
        raw = generator.complete(EXTRACT_SYSTEM, prompt, max_tokens=400, temperature=0.2)
    except Exception:
        raw = ""
    data = parse_json(raw) or {}
    return {
        "summary": (str(data.get("summary") or "").strip())[:60]
                   or user_text.strip()[:30],
        "emotion": emotion_mod.normalize(data.get("emotion")) if data.get("emotion")
                   else emotion_mod.guess(user_text),
        "importance": _clamp_importance(data.get("importance", 3)),
        "facts": _as_list(data.get("facts")),
        "preferences": _as_list(data.get("preferences")),
        "relations": _as_list(data.get("relations")),
        "raw_ok": bool(data),
    }


# 常见关系词。用于 `_enrich_relation` 的**反向绑定**。
# 实证（2026-10-15，`tests/analysis/relation_extract_probe.py` 实测真实抽取）：
#   用户「我妹妹叫林小雨」→ 模型给 relations=['林小雨']（**只有名字，没有关系词**）
#   用户「我妹妹在读大学」→ relations=['妹妹']（**只有关系词，没有名字**）
# 原实现只处理「有关系词 + 原话里有叫XX」这一种，于是：
#   · relations 已经是名字时，`叫XX` 抽出的名字与 body 相同 → 被 `n != body` 滤掉
#     → 直接返回 body → **落库成「对方提到的人：林小雨」，没有括号**。
#   · 而一跳扩展（`engine/retrieve.py: relation_targets`）认的是 `名字（关系）`。
#     → 锚点链路**实际从来没接通过**（先前的 2/3 是手写理想数据跑出来的假象）。
_REL_WORDS = (
    "妹妹", "姐姐", "哥哥", "弟弟", "妈妈", "母亲", "爸爸", "父亲",
    "老婆", "妻子", "老公", "丈夫", "女朋友", "男朋友", "对象",
    "同事", "同学", "朋友", "老师", "室友", "邻居", "亲戚",
    "儿子", "女儿", "孩子", "爷爷", "奶奶", "姥姥", "姥爷", "外公", "外婆",
    "宠物", "猫", "狗",
    "公司", "单位", "学校", "大学", "医院", "健身房",
)
# 名字与关系词的距离上限：隔太远就是两件事了，别硬绑
_REL_MAX_GAP = 6


def _nearest_relation_word(text: str, name: str) -> str:
    """在原文里找离 `name` 最近的关系词（反向绑定用）。太远就不算。"""
    pos = text.find(name)
    if pos < 0:
        return ""
    best, best_d = "", 10 ** 9
    for word in _REL_WORDS:
        for m in re.finditer(re.escape(word), text):
            # 关系词不能就在名字内部（如名字含"大学"）
            if m.start() <= pos < m.end():
                continue
            d = abs(m.start() - pos)
            if d < best_d:
                best, best_d = word, d
    return best if best_d <= _REL_MAX_GAP else ""


def _enrich_relation(content: str, user_text: str) -> str:
    """关系尽量补成 `名字（关系）` 形态。

    这个形态不只是好看：**一跳扩展靠它当锚点**
    （`engine/retrieve.py: relation_targets` 解析 `名字（关系）`）。
    两种方向都要处理，因为实测模型给哪一种都有：
        正向：body=「妹妹」+ 原话「我妹妹叫林小雨」  → 「林小雨（妹妹）」
        反向：body=「林小雨」+ 原话「我妹妹叫林小雨」→ 「林小雨（妹妹）」
    2026-10-15 之前只做了正向，而模型实际更常给**名字**，于是永远没有括号。
    """
    body = str(content or "").strip()
    text = str(user_text or "")
    if not body:
        return body
    if "（" in body and "）" in body:
        return body                      # 已经是目标形态，别叠加

    names = [n for n in find_called_names(text) if n != body]
    if names:
        # 正向：body 是关系词，原话里有"叫XX"
        if body in text:
            anchor = text.find(body)
            best = min(names, key=lambda n: abs(text.find(n) - anchor))
        else:
            best = names[0]
        return f"{best}（{body}）"

    # 反向：body 本身就是名字（模型常见的给法）→ 找最近的关系词补上
    if body not in _REL_WORDS and body in text:
        word = _nearest_relation_word(text, body)
        if word:
            return f"{body}（{word}）"
    return body


# 谓语的**起始字**。`_canonical` 只给"看起来是陈述句"的内容补主体。
# ⚠ 必须按**首字**判断，不能用子串：实测「广州大学」含「学」，
#   用子串判会把它当谓语，于是拼出「对方广州大学」——
#   **凭空断言"广州大学是你的"**，而 `_grounded` 只查词是否出现过，查不出这种主体错位。
#   （实测来源：用户说「小雨是广州大学的学生」，模型只抽出裸名词「广州大学」。）
_PREDICATE_START = set(
    "对在有害会要想爱住做学吃喝睡去来买养叫搬上下不没能"
    "工打计需觉开过毕讨喜"
)


def _is_bare_entity(text: str) -> bool:
    """这串看起来是不是"以名词开头、没有谓语"——**只给 `_canonical` 用**。

    它的唯一用途：决定要不要给它补主体。「广州大学」补成「对方广州大学」
    就是**凭空断言**"广州大学是你的"，所以裸名词不补。

    ⚠ **按首字判断就够了，不要加码成"整串找谓语"**（2026-10-16 踩过）：
      改成整串找之后，「广州大学」因为含「学」（也是「学习」的首字）
      又被判成"陈述"；而「妹妹喜欢喝茶」这类名词主语+谓语的又判不出来。
      两头不讨好。**而且这个判据千万不要再拿去当"要不要存"的闸门**——
      实测它会顺手把「妹妹在广州上学」这种正常事实整条丢掉（误杀 = 丢记忆）。
    """
    t = str(text or "").strip()
    if not t:
        return True
    if t.startswith(("对方", "用户")):
        return False                       # 已经有主体
    return t[0] not in _PREDICATE_START


def _canonical(kind: str, content: str) -> str:
    """把记忆存成规范句：补上主体，检索才容易命中。

    实测问题：存成"学习吉他"，问"我在学什么乐器"时召不回来（缺主体、缺上下文）；
    存成"对方学习吉他"后，两种问法都容易命中。

    ⚠ 但**裸名词不能补主体**（2026-10-15 修）：「广州大学」补成
      「对方广州大学」等于凭空断言"广州大学是你的"。裸实体原样返回，
      由 `remember_turn` 判为"不是事实"跳过——它通常也已经作为 relation 存了。
    """
    text = str(content or "").strip()
    if not text:
        return text
    # 主体写错的重灾区："我叫林一""我住在杭州"——这是**对方**的事。
    # 写成"我"会让模型以为是自己的（实测据此自称"你叫未央吧"）。先把"我"换成"对方"。
    if text.startswith("我"):
        text = "对方" + text[1:]
    if text.startswith(("用户", "对方", "你", "他", "她")):
        return text
    if kind == "relation":
        return f"对方提到的人：{text}"
    if _is_bare_entity(text):
        return text                        # 裸实体：不补主体，避免假断言
    # 偏好类常见写法（喜欢/讨厌/不喝…）已经有谓语，直接补主体
    return f"对方{text}"


# 第三方主语词：原话以这些**开头**时，说的是别人（家人/宠物/单位），不是用户自己。
# 只用于「这句话的主语是谁」的判断，不参与别的逻辑。
_THIRD_WORDS = ("妹妹", "姐姐", "哥哥", "弟弟", "妈妈", "爸爸", "母亲", "父亲",
                "同事", "朋友", "同学", "室友", "邻居", "男友", "女友", "对象",
                "老婆", "老公", "儿子", "女儿", "外甥", "侄女", "侄子", "叔叔",
                "阿姨", "舅舅", "姑姑", "爷爷", "奶奶", "姥姥", "姥爷", "亲戚",
                "猫", "狗", "宠物", "公司", "单位", "学校", "老师", "医生")

_CLAUSE_SPLIT = re.compile(r"[，。；！？,;!?\n]")


def _names_from_relations(relations) -> list[str]:
    """从本轮 relations（`对方提到的人：林小雨（妹妹）` 这种串）里抠出名字。"""
    out: list[str] = []
    for raw in (relations or []):
        for part in str(raw or "").replace("，", "（").split("（"):
            part = part.strip("） 　(")
            for prefix in ("对方提到的人：", "对方", "用户"):
                if part.startswith(prefix):
                    part = part[len(prefix):]
                    break
            part = part.strip()
            if len(part) >= 2 and part not in out:
                out.append(part)
    return out


def _grams_of(text: str) -> set[str]:
    text = str(text or "")
    return {text[i:i + 2] for i in range(len(text) - 1)
            if all("\u4e00" <= ch <= "\u9fff" for ch in text[i:i + 2])}


def _third_party_subject(user_text: str, relations, body: str = "") -> bool:
    """原话这句是不是**以别人为主语**、而且**说的正是这条内容**。

    两层判据：

      ① **主语是别人** → 直接算。注意「**我**妹妹在公司上班」也算——
         句首的「我」是**领属**（我的妹妹），真正的主语是妹妹，
         所以要先把开头的「我/我的/咱」剥掉再判。
      ② 还要看**这条内容出自哪一句**：只有覆盖它的那个分句主语是别人时才算。

    ⚠ 第②层必须带"覆盖"这个条件，否则会误杀：
      「我妹妹叫林小雨，我最近很累」里有第三方，但 `对方最近很累`
      说的是**用户自己那一句**——不覆盖第三方分句就放行。
      误杀的代价是丢记忆，比漏拦更糟。
    ⚠ 判"出自哪一句"用**比较**而不是阈值（实测两次踩坑）：
      · 用阈值（≥0.8）会被**改写**绕过：用户说「小雨是广州大学的学生」，
        抽取写「用户**就读于**广州大学」——二元组只重合 0.5，拦不住。
      · 用**全集**又会被跨界二元组卡住（`大学学生` 产生分句里没有的 `学学`）。
      所以改成：算出每个分句与这条内容的**重叠率**，
      **覆盖它的那个分句（重叠最高的）主语是别人** → 拒写；
      且**必须严格高于**所有"用户自己"分句的重叠率——
      否则一句里同时有第三方和自己时（重叠并列）会误杀。
    """
    names = _names_from_relations(relations)
    body_grams = _grams_of(body)

    def is_third(clause: str) -> bool:
        head = clause.strip()
        for lead in ("我的", "我", "咱的", "咱"):
            if head.startswith(lead):
                head = head[len(lead):]
                break
        return (any(head.startswith(n) for n in names)
                or any(head.startswith(w) for w in _THIRD_WORDS))

    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(str(user_text or ""))
               if c.strip()]
    if not body_grams:
        # 没实词可判：只要有分句的主语是别人，就按"说的是别人"算
        return any(is_third(c) for c in clauses)
    third_best = 0.0
    other_best = 0.0
    for clause in clauses:
        clause_grams = _grams_of(clause)
        if not clause_grams:
            continue
        overlap = len(body_grams & clause_grams) / len(body_grams)
        if is_third(clause):
            third_best = max(third_best, overlap)
        else:
            other_best = max(other_best, overlap)
    return third_best > other_best and third_best > 0


def _starts_with_other(body: str, relations) -> bool:
    """正文开头紧跟的是另一个实体（`妹妹叫林小雨`）→ 这条说的其实是那个人。"""
    names = _names_from_relations(relations)
    b = str(body or "")
    if any(b.startswith(n) for n in names):
        return True
    return any(b.startswith(w) for w in _THIRD_WORDS)


def _subject_ok(content: str, user_text: str, relations) -> bool:
    """内容的**主体**必须与原话的主体对得上。

    实测问题（2026-10-16）：用户说「小雨是广州大学的学生」，
    抽取给出 `用户是广州大学学生` —— **把第三方的事记成了用户自己的**。
    这种错**比抽不到更糟**：它会污染画像，而且多跳会跳到错的人身上。

    判据（保守，只拦明确错位）：
      · 内容主体不是"用户/对方" → 别的闸门管，放行
      · 主体后面紧跟另一个实体（`对方妹妹叫…`）→ 这条说的就是那个人，放行
      · 剩余情况 = 断言"用户自己的事"；若**原话的主语是别人** → 拒写

    ⚠ 已知局限：原话分句**不以**第三方开头、但中间夹了第三方时判不出
      （如「我妹妹对花生过敏」里的主体其实也不是用户）。宁可少拦，
      也不要把正常事实误杀——误杀的代价是丢记忆。
    """
    body = str(content or "")
    if not body.startswith(("对方", "用户")):
        return True
    rest = body[2:]
    if _starts_with_other(rest, relations):
        return True
    return not _third_party_subject(user_text, relations, rest)


# 绝对化词：模型常把"有时候"放大成"永远"、把"喜欢"放大成"只喜欢"。
# 内容里有这些词、而用户原话里没有 → 这条被放大了范围，拒写。
# ---- 状态句被误归成「关系」时还原成事实（2026-10-16）----
# 实测（Qwen3.5-2B 抽取器，8 句里 3 句）：
#     用户「林小雨对花生过敏」→ facts=['林小雨过敏'] ＋ relations=['林小雨（过敏对象）']
#        ⚠ **宾语「花生」整个丢了** → 问「我妹妹对什么过敏」答不出（块里就没有花生）
#     用户「我妹妹怕狗」    → facts=[] ＋ relations=['妹妹（怕狗）']
#     用户「我住在南京」    → 两边都空（**整句丢失**）
#
# 关系该是**角色**（妹妹/同事/宠物/单位），而「过敏」「怕狗」是**状态**——
# 状态句是**事实**。所以：把这种误归类的"关系"改写成事实，
# **从用户原话里把那个分句捞回来**（用原话，不引入编造风险）。
#
# ⚠ 「上学的地方」「上班的地方」这类**看着像状态、其实是角色**的要放行，
#   否则会把好的地点关系也拆掉（项目里 `工业软件公司（上班的地方）` 就是这么用的）。
_ROLE_HINTS = ("地方", "地点", "单位", "学校", "公司", "住址", "住所",
               "城市", "老家", "家人", "主人", "医生", "老师")
# ⚠ 这里**不要放「对象」**：本意是"男朋友/对象"，但「过敏**对象**」会撞上，
#   于是状态句被判成角色、还原不出来（实测踩过）。有歧义的词一律不放。
_STATE_MULTI = ("过敏", "喜欢", "讨厌", "住在", "上班", "上学", "读书", "工作")
# 单字状态词**只按开头**算：否则「学校」会被「学」误伤
_STATE_START = set("怕吃喝住爱想养")


def _is_state_word(word: str) -> bool:
    """`名字（X）` 里这个 X 是不是状态词（而不是角色）。"""
    w = str(word or "").strip()
    if not w:
        return False
    if any(h in w for h in _ROLE_HINTS):
        return False                       # 是角色/类型（上学的地方、单位…）
    if any(s in w for s in _STATE_MULTI):
        return True
    return w[0] in _STATE_START


def _clause_with(user_text: str, name: str, limit: int = 24) -> str:
    """用户原话里**同时含这个名字**的那个分句（短一点，避免整段搬进来）。"""
    for clause in _CLAUSE_SPLIT.split(str(user_text or "")):
        c = clause.strip()
        if c and name in c and len(c) <= limit:
            return c
    return ""


def _reclassify_state_relations(relations, user_text: str):
    """把 `名字（状态词）` 这种误归类的关系**还原成事实**。

    返回 `(保留的关系, 还原出的事实)`：
      · 状态词当关系词的 → 从原话里捞那个分句当事实，**该关系不再保留**
        （它本来就不是关系，留着还会让 `known_names()` 把「怕狗」当专名）
      · 捞不到分句的 → 关系原样保留（宁可留着也不凭空造）
    """
    kept: list[str] = []
    recovered: list[str] = []
    for raw in (relations or []):
        text = str(raw or "")
        m = _REL_PAIR_RE.match(text.replace("对方提到的人：", "", 1).strip())
        if not m or not _is_state_word(m.group(2)):
            kept.append(text)
            continue
        name = m.group(1).strip()
        clause = _clause_with(user_text, name)
        if clause:
            recovered.append(clause)
        else:
            kept.append(text)              # 捞不到就别动它
    return kept, recovered


def _complete_state_facts(facts, user_text: str) -> tuple[list[str], list[str]]:
    """带状态词却**没有宾语**的事实，从原话补全。

    返回 `(处理后的事实, 补全过的)`。

    ⚠ 这是 `_reclassify_state_relations` 的**盲区补丁**（实测踩过）：
      抽取丢宾语有两种形态——
        ① `relations=['林小雨（过敏对象）']` → 上面那条路抓得到
        ② `facts=['林小雨过敏']` ＋ `relations=['林小雨']` → **上面那条路抓不到**
           （没有"状态词关系"可当线索），于是宾语照样丢。
    判据：事实里含状态词，且状态词**前面**那段没有任何宾语标志
    （对/在/喝/吃/喜欢/和/跟/的）→ 说明宾语被吞了 → 从原话捞那个分句。
    """
    out: list[str] = []
    filled: list[str] = []
    for raw in (facts or []):
        text = str(raw or "")
        hit = next((s for s in _STATE_MULTI if s in text), "")
        if not hit:
            out.append(raw)
            continue
        # ⚠ 状态词必须在**结尾**：宾语被吞掉时它才会留在末尾。
        #   否则会误伤「妹妹**喜欢喝**美式」这种——它的宾语在状态词**后面**，
        #   本来就不缺（实测踩过，整句被无谓地替换掉）。
        if not text.endswith(hit):
            out.append(raw)
            continue
        pre = text.split(hit)[0]
        # ⚠ 顺序很重要：**先剥主体前缀、再查宾语标志**。
        #   反过来的话，「**对方**过敏」会因为「对方」里含「对」而被判成
        #   "已经有宾语"直接跳过（实测踩过）。
        name = pre
        for pfx in ("对方提到的人：", "对方", "用户"):
            if name.startswith(pfx):
                name = name[len(pfx):]
                break
        name = name.strip()
        if any(m in name for m in ("对", "在", "喝", "吃", "喜欢", "和", "跟", "的")):
            out.append(raw)                # 已经有宾语，别动
            continue
        if name:
            clause = _clause_with(user_text, name)
        else:
            # 主语是"对方/用户"（被规范掉了）→ 找**以状态词结尾**的那个分句
            clause = next((c.strip() for c in _CLAUSE_SPLIT.split(user_text)
                           if c.strip().endswith(hit)
                           and len(c.strip()) > len(text)), "")
        if clause and hit in clause and len(clause) > len(text):
            out.append(clause)             # 用**原话**补全，不引入编造
            filled.append(clause)
        else:
            out.append(raw)
    return out, filled


ABSOLUTE_TERMS = ("只", "永远", "从不", "从来", "一定", "完全", "绝对",
                  "以后都", "不再", "再也不", "总是", "每次都")


def _absolute_ok(content: str, user_text: str) -> bool:
    """记忆里的绝对化说法，必须能在用户原话里找到出处。"""
    body = str(content or "")
    for prefix in ("对方提到的人：", "对方", "用户"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    text = str(user_text or "")
    for term in ABSOLUTE_TERMS:
        if term in body and term not in text:
            return False
    return True


def _grounded(content: str, user_text: str) -> bool:
    """这条提取结果是不是"有出处"——它得和用户这轮的原话沾点边。

    实测问题：提取时会冒出用户**从没说过**的内容，例如用户说"从南京搬到成都"，
    库里却存下 `用户住在北京市`；之后每轮都当事实注入，模型就答"北京"。
    判断方式用最朴素的一条：至少有一个中文二字片段重合。
    """
    body = str(content or "")
    for prefix in ("对方提到的人：", "对方", "用户"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    grams = {body[i:i + 2] for i in range(len(body) - 1)}
    grams = {g for g in grams if len(g) == 2 and all("\u4e00" <= ch <= "\u9fff" for ch in g)}
    if not grams:
        return True                     # 没有中文片段（纯数字/代号）就不拦
    text = str(user_text or "")
    return any(gram in text for gram in grams)


_REL_PAIR_RE = re.compile(r"^(.+?)（(.+?)）$")


def _strip_invented_name(content: str, user_text: str) -> str:
    """`名字（关系）` 里的**名字必须在原话里出现过**，否则是编造 → 只留关系词。

    ⚠ 实测（2026-10-15，`tests/analysis/relation_extract_probe.py`）：
    改了提示词、要求 relations 写成「名字（关系）」之后，用户只说
    「我妹妹在读大学」（**整句没有名字**），模型却输出「**林小雨**（妹妹）」——
    `林小雨` 在微调数据里出现太多次，模型把它**补全**出来了。

    `_grounded()` 拦不住这种情况：它只看 body 里**有没有任意一个**二字片段在原话里，
    而 body「林小雨（妹妹）」里的「妹妹」是在原话里的 → 判定"有出处"通过。
    **接地的那部分掩护了编造的那部分。**

    编造的名字一旦入库，比没有锚点更糟：以后检索会把一个用户从没提过的人
    当成事实注入。所以这里**降级而不是整条丢弃**——保留「妹妹」这个真实信息，
    只砍掉编造的名字。
    """
    body = str(content or "").strip()
    m = _REL_PAIR_RE.match(body)
    if not m:
        return body
    name, relation = m.group(1).strip(), m.group(2).strip()
    if not name or not relation:
        return body
    # 名字要在原话里可查。
    # ⚠ 判据是「**整名命中**，或**首尾两个二字片段都有出处**」——
    #   不能写成"任一二字片段命中"：那样太松，实测放行过被污染的
    #   「林小**记得**（妹妹）」（原话是"我妹妹叫**林小雨**"，回答里有"记住"，
    #   模型把两者粘在一起；"林小"在原话里有 → 松判据放行）。
    #   而「工业软件公司（上班的地方）」原话是"做工业软件的公司"（**非连续**），
    #   首（工业）尾（公司）都有出处 → 仍能正确放行。
    text = str(user_text or "")
    if name in text:
        return body
    if len(name) <= 2:
        return body if name in text else relation
    head, tail = name[:2], name[-2:]
    if head in text and tail in text:
        return body
    return relation


def _is_self_name(content: str) -> bool:
    """这条"关系"是不是 AI 自己的名字/自称。

    实测出现过：未央（AI 自己的名字）被当成"对方提到的人"写进用户记忆，
    之后每轮检索都排在前面——等于用 AI 的档案污染用户记忆。
    """
    text = str(content or "").strip()
    if not text:
        return False
    names = {"未央", "你", "我"}
    try:
        import yaml
        path = (pathlib.Path(__file__).resolve().parent.parent
                / "config" / "ai_profile.yaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        name = str((data.get("identity") or {}).get("name") or "").strip()
        if name:
            names.add(name)
    except Exception:
        pass
    return any(name and (text == name or text.endswith(name)) for name in names)


def _union_infos(infos: list[dict]) -> dict:
    """把多次抽取按字段并起来（并记 agreement：几次都抽到了）。"""
    if not infos:
        return {}
    if len(infos) == 1:
        out = dict(infos[0])
        out["agreement"] = {}
        out["passes"] = 1
        return out
    merged = dict(infos[0])                 # summary/emotion/importance 取第一次
    agreement: dict[str, int] = {}
    for key in ("facts", "preferences", "relations"):
        counts: dict[str, int] = {}
        order: list[str] = []
        for info in infos:
            seen = set()
            for item in (info.get(key) or []):
                s = str(item)
                if s not in counts:
                    counts[s] = 0
                    order.append(s)
                if s not in seen:           # 同一次里重复只算一次
                    counts[s] += 1
                    seen.add(s)
        merged[key] = order
        for s, n in counts.items():
            agreement[f"{key}:{s}"] = n
    merged["agreement"] = agreement
    merged["passes"] = len(infos)
    return merged


def extract_turn_union(generator, user_text: str, reply_text: str,
                       passes: int = 2) -> dict:
    """抽 `passes` 次取**并集**——专门对付**模型抖动**。

    ## 为什么要这么做（2026-10-16 实测）

    `tests/analysis/extract_metric.py` 定位出：抽取器剩下的毛病**主要是抖动**，
    不是稳定错——同一句「林小雨对花生过敏」，有时抽出 `花生`、有时只留 `过敏`。

    抖动改不了模型，但**程序侧能兜**：抽两次，两次里只要有一次抽对就救回来。
    实测（`tests/analysis/extract_union_probe.py`，每句 6 次）：

        「每次都对」的条目：单次 **8/12** → 并集 **11/12**
        编造条数：          单次 0     → 并集 **0**（并集没有引入脏东西）

    救回来的：花生 4/6→3/3、海鲜 5/6→3/3、后端 5/6→3/3、南京 3/6→2/3。

    代价：抽取耗时翻倍（2B 上 2.3s → 4.6s）。**但抽取发生在回复之后**，
    用户无感；而漏抽的记忆是**永久**丢的。这笔账划算。

    ⚠ 并集只并**字段内容**，不并 summary/emotion（那两个取**第一次**的，
      避免不同次之间来回跳）。并按 `agreement` 记下"几次都抽到了"——
      **这比模型自报的 conf 可靠得多**（自报那个实测 84% 都 ≥0.9、不区分好坏）。
    """
    passes = max(1, int(passes))
    infos = [extract_turn(generator, user_text, reply_text) for _ in range(passes)]
    return _union_infos(infos)


def remember_turn(memory: Memory, generator, user_text: str, reply_text: str,
                  session: str = "", turn: int = 0, index=None,
                  profile_path: pathlib.Path | str | None = None,
                  skill: str = "", level: str = "",
                  emotion_info: dict | None = None) -> dict:
    """认知提取 → 写情景记忆 → 写语义记忆 → 写向量索引 → 更新画像。

    profile_path 为 None 时写默认的 cognition/profile.json（测试可传独立路径）。
    重要度取「模型判断」与「规则初始赋值」的较大者：
    显式信息、情绪强烈的事件会被抬高，普通闲聊保持低（改进 13）。
    返回本轮提取结果（含 episode_id）。
    """
    from cognition import decay as decay_engine

    # 抽取：默认**抽两次取并集**（对付模型抖动，实测 8/12 → 11/12、且不引编造）。
    # 次数由 config/memory.yaml 的 `extract_passes` 控制（设 1 就退回单次）。
    _passes = 2
    # 抽取策略：**默认 `split`**（拆两个提示词各一次，见 TEMPLATE_FACTS 上方的对比）。
    # 也可以切回 `union`（同提示词跑两次取并集）做 A/B。
    # 配置：config/memory.yaml 的 `extract_strategy`；环境变量 COMPANION_EXTRACT_STRATEGY 可临时覆盖。
    _strategy = "split"
    _passes = 2
    try:
        import yaml as _yaml
        _cfg = _yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent
                                / "config" / "memory.yaml")
                               .read_text(encoding="utf-8")) or {}
        _strategy = str(_cfg.get("extract_strategy", "split") or "split").strip()
        _passes = max(1, int(_cfg.get("extract_passes", 2)))
    except Exception:
        _strategy, _passes = "split", 2
    if os.environ.get("COMPANION_EXTRACT_STRATEGY"):
        _strategy = os.environ["COMPANION_EXTRACT_STRATEGY"].strip()
    if os.environ.get("COMPANION_EXTRACT_PASSES"):
        try:
            _passes = max(1, int(os.environ["COMPANION_EXTRACT_PASSES"]))
        except Exception:
            pass
    if _strategy == "union":
        info = extract_turn_union(generator, user_text, reply_text, passes=_passes)
    else:
        info = extract_turn_split(generator, user_text, reply_text)
    info.setdefault("summary", "")
    info.setdefault("emotion", "")

    # **回读校验**：拿原话核对抽到的条目，看有没有"整句没着落"。
    #
    # ⚠ 它是**叠加**在并集之上的，**不是替换**（2026-10-16 实测定的）：
    #   并集救回来的 4 条里有 **3 条是"丢一半"（partial）**——
    #   而回读校验的闸门**只认"整句没着落"**（uncovered），
    #   因为按"丢一半"报的精确率只有 20%（抽对了也报），
    #   拿它当闸门 = 每轮都重抽 = 等于没省。
    #   所以：**并集负责常态抖动，回读校验负责补"整句掉了"这种极端情况**。
    try:
        from cognition import verify as _verify
        cov = _verify.coverage(user_text, info)
        if _verify.should_retry(cov) and _passes < 3:
            info = _union_infos(
                [info, extract_turn(generator, user_text, reply_text)])
            cov = _verify.coverage(user_text, info)
        info["coverage"] = cov
    except Exception:
        info["coverage"] = None

    # **把用户的回答关联回上一轮那个待确认的问题**（2026-10-18）。
    # 组装层在生成回复**之前**把"不确定的记忆"以提问方式抛出去并登记；
    # 这里看这一轮用户说了什么，据此把那条记忆**升为确定 / 否掉**。
    # ⚠ 只认**更早轮次**问出去的（`before_turn`）——同一轮里提问紧接着就解决的话，
    #   会把"触发提问的那句话"当成回答，每条待确认记忆都会被立刻误判。
    try:
        from cognition import clarify as _clarify
        info["clarify"] = _clarify.resolve_pending(
            memory, user_text, session=session, turn=turn)
        memory.expire_questions(keep_open=6, session=session)
    except Exception as exc:
        info["clarify"] = {"error": f"{type(exc).__name__}: {exc}"}
    rules = decay_engine.load_rules()
    rule_importance = decay_engine.initial_importance(
        user_text, emotion_info,
        repeated=bool(info.get("facts") or info.get("preferences")),
        rules=rules)
    importance = int(max(int(info["importance"]), round(rule_importance)))
    importance = max(1, min(5, importance))
    info["importance"] = importance
    info["rule_importance"] = rule_importance
    episode_id = memory.add_episode(
        user_text=user_text, reply_text=reply_text,
        summary=info["summary"], emotion=info["emotion"],
        importance=importance, session=session, turn=turn, skill=skill,
        level=level,
    )
    info["episode_id"] = episode_id
    memory.log("write", "episodic", episode_id,
               f"{info['summary'][:40]}（重要度 {importance}）")
    # 记忆关联（改进 20）：与提到同一个人/宠物的记忆连起来
    try:
        memory.link_by_entity(f"episodic#{episode_id}",
                              [user_text, info.get("summary") or ""]
                              + list(info.get("facts") or []))
    except Exception:
        pass

    # 向量索引条目：情景用 e{id}，语义用 s{id}
    vector_items: list[tuple[str, str]] = [
        (f"e{episode_id}", f"{user_text}｜{info['summary']}")
    ]

    # ---- 状态句被误归成「关系」→ 还原成事实（否则宾语会丢，实测见函数注释）----
    info["relations"], _recovered = _reclassify_state_relations(
        info.get("relations") or [], user_text)
    if _recovered:
        info["facts"] = list(info.get("facts") or []) + _recovered
        memory.log("fix", "semantic", None,
                   f"状态句被误归成关系，已从原话还原成事实："
                   f"{'；'.join(_recovered)[:40]}")
    # 另一条路：事实里带状态词却没有宾语（「林小雨过敏」缺「花生」）→ 从原话补全
    info["facts"], _filled = _complete_state_facts(
        info.get("facts") or [], user_text)
    if _filled:
        memory.log("fix", "semantic", None,
                   f"状态句缺宾语，已从原话补全：{'；'.join(_filled)[:40]}")

    for kind, key in (("fact", "facts"), ("preference", "preferences"),
                      ("relation", "relations")):
        for content in info[key]:
            if kind == "relation" and _is_self_name(content):
                # AI 自己的名字/自称不是"对方提到的人"，不能写进用户记忆
                memory.log("skip", "semantic", None, f"跳过自称关系：{content[:20]}")
                continue
            content = _canonical(kind, content)
            if kind == "relation":
                content = _canonical(kind, _enrich_relation(
                    content.replace("对方提到的人：", ""), user_text))
                # 名字必须真有出处：模型会凭训练里的常见名字"补全"出不存在的名字
                # （实测「我妹妹在读大学」→「林小雨（妹妹）」）。降级只留关系词。
                stripped = _strip_invented_name(
                    content.replace("对方提到的人：", ""), user_text)
                if stripped != content.replace("对方提到的人：", ""):
                    memory.log("fix", "semantic", None,
                               f"关系里的名字无出处，降级为：{stripped[:20]}")
                    content = _canonical(kind, stripped)
            if not _grounded(content, user_text):
                memory.log("skip", "semantic", None,
                           f"跳过无出处的内容（用户没说过）：{content[:24]}")
                continue
            if not _absolute_ok(content, user_text):
                memory.log("skip", "semantic", None,
                           f"跳过被放大的说法（绝对化词无出处）：{content[:24]}")
                continue
            # 主体校验：**只对事实/偏好**——关系条目天生不是"关于用户的断言"。
            # ⚠ 关系条目必须豁免：`对方提到的人：林小雨（妹妹）` 说的是
            #   "用户提到了这个实体"，不是"这是用户的事"。
            #   第一版忘了豁免 → 把多跳最需要的**锚点条目全拦掉了**（实测踩过，
            #   症状是"记忆莫名其妙变少"，日志里三条关系全被标成主体错位）。
            # 另一面（2026-10-16）：用户说「小雨是广州大学的学生」，
            # 抽取给出 `用户是广州大学学生`——把第三方的事记成了用户自己的。
            # 这种错比抽不到更糟：污染画像，而且多跳会跳到错的人身上。
            if kind != "relation" and not _subject_ok(
                    content, user_text, info.get("relations") or []):
                memory.log("skip", "semantic", None,
                           f"跳过主体错位（原话说的是别人）：{content[:24]}")
                continue
            semantic_id, created = memory.upsert_semantic(
                kind, content, confidence=0.5, episodic_id=episode_id,
                emotion=info.get("emotion") or ""
            )
            if created:
                vector_items.append((f"s{semantic_id}", content))
                memory.log("write", "semantic", semantic_id, f"[{kind}] {content[:40]}")

    if index is not None:
        try:
            index.add_texts(vector_items)
        except Exception:
            # 嵌入服务不可用不该影响对话：检索会自动退回关键词匹配
            pass

    # ---- 收尾：把本轮新产生的**未决冲突**交给模型合并（改进 18 的初衷）----
    # 冲突检测的初衷是**合并相关记忆**，但原来只做到"把新说法藏起来等用户拍板"，
    # 而用户几乎不回答 → 新说法永久丢失。现在：冲突条目不隐身（memory 侧已改），
    # 这里再用模型把「旧 + 新」合成一条；**失败就保持未决**（两条都在、都可检索）。
    # ---- 学别名：拿已知全名去用户原话里找简称 ----
    # 「林小雨」的末 2 字是「小雨」，但**必须用户真的这么叫过**才算证据
    # （规则在 `cognition/alias.py`；这里只负责在正确的时机调它）。
    # 放在语义写完之后：本轮新出现的全名也能立刻参与。
    try:
        from cognition import alias as alias_mod
        info["aliases"] = alias_mod.learn_from_turn(
            memory, user_text,
            relations=[str(x) for x in (info.get("relations") or [])],
            episodic_id=int(locals().get("episode_id") or 0))
        # 规则抓不到的换法（阿雨、小林子…）交给模型指认：
        # **模型只做闭集选择**（从已知的人里挑一个或答"新"），
        # 写入与否由 `add_alias` 的护栏裁决（见 cognition/alias.py 的说明）。
        info["alias_proposals"] = alias_mod.model_propose_aliases(
            memory, generator, user_text,
            relations=[str(x) for x in (info.get("relations") or [])],
            episodic_id=int(locals().get("episode_id") or 0))
    except Exception as exc:
        info["aliases"] = []
        info["alias_proposals"] = []
        memory.log("alias", "alias", 0, f"学别名时出错（忽略）：{exc}")

    # ⚠ 合并是破坏性的，护栏在 `cognition/merge.py` 里（数字/实词接地/长度/主体），
    #   模型不可用或被拒 → 什么都不改。
    #
    # ⚠⚠ **默认关掉**（2026-10-19，见 `config/memory.yaml` 的 `merge_conflicts`）：
    #   用户实测"重复询问时 4B 会把主体搞乱"，怀疑就是合并造成的——
    #   `MERGE_TEMPLATE` 明确要求「合并成**一句**最准确的话」，
    #   **两条事实压成一句**，句子里就出现**多个主体/多个谓词**，
    #   小模型读到这种复合句容易把「谁对什么」对错号。
    #   关掉**不丢记忆**：冲突条目照样并存、打 `conflict_with`，
    #   块的「同一件事有两种说法：新的…旧的…」照旧标注（那是组装的活，与合并无关）。
    _merge_on = False
    try:
        import yaml as _yaml2
        _cfg2 = _yaml2.safe_load((pathlib.Path(__file__).resolve().parent.parent
                                  / "config" / "memory.yaml")
                                 .read_text(encoding="utf-8")) or {}
        _merge_on = bool(_cfg2.get("merge_conflicts", False))
    except Exception:
        _merge_on = False
    if os.environ.get("COMPANION_ENABLE_MERGE") == "1":
        _merge_on = True
    if _merge_on:
        try:
            info["merges"] = resolve_conflicts_with_model(memory, generator, limit=2)
        except Exception as exc:
            info["merges"] = []
            memory.log("merge", "semantic", None, f"合并步骤异常（忽略）：{exc}")
    else:
        info["merges"] = []

    info["profile"] = update_profile(info, turn, path=profile_path or PROFILE_PATH)
    return info


def resolve_conflicts_with_model(memory, generator, limit: int = 2) -> list[dict]:
    """把未决冲突交给模型合并；**每一步失败都什么都不改**。

    返回每次尝试的结果（便于观测与测试）：
        {"new_id", "old_id", "action": "merged"/"unresolved", "reason", "merged"}
    """
    from cognition.merge import apply_merge, merge_pair

    out: list[dict] = []
    for item in memory.pending_conflicts(limit=limit):
        new_id = int(item["new_id"])
        old_id = int(item.get("old_id") or 0)
        if not old_id:
            continue
        got = merge_pair(generator, item.get("old") or "", item.get("new") or "")
        record = {"new_id": new_id, "old_id": old_id, "action": got["action"],
                  "reason": got.get("reason", ""), "merged": got.get("merged", "")}
        if got["action"] == "merged":
            apply_merge(memory, new_id, old_id, got["merged"])
        else:
            memory.log("merge_skip", "semantic", new_id,
                       f"未合并 #{old_id}（{got.get('reason')}）："
                       f"两条都保留、都可检索")
        out.append(record)
    return out


def load_profile(path: pathlib.Path | str = PROFILE_PATH) -> dict:
    path = pathlib.Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def update_profile(info: dict, turn: int = 0,
                   path: pathlib.Path | str = PROFILE_PATH) -> dict:
    """把本轮提取到的东西累加进 cognition/profile.json。"""
    path = pathlib.Path(path)
    profile = load_profile(path)
    stamp = dt.datetime.now().isoformat(timespec="seconds")

    profile.setdefault("created_at", stamp)
    profile["updated_at"] = stamp
    profile["turns"] = int(profile.get("turns", 0)) + 1

    buckets = {"facts": info.get("facts") or [],
               "preferences": info.get("preferences") or [],
               "relations": info.get("relations") or []}
    for name, items in buckets.items():
        store = profile.setdefault(name, [])
        index = {entry["content"]: entry for entry in store if "content" in entry}
        for content in items:
            content = content.strip()
            if not content:
                continue
            if content in index:
                entry = index[content]
                entry["count"] = int(entry.get("count", 1)) + 1
                entry["last_seen"] = stamp
                entry["confidence"] = min(0.95, float(entry.get("confidence", 0.5)) + 0.08)
            else:
                entry = {"content": content, "confidence": 0.5, "count": 1,
                         "first_seen": stamp, "last_seen": stamp}
                store.append(entry)
                index[content] = entry

    counts = profile.setdefault("emotion_counts", {})
    emotion = info.get("emotion") or "平静"
    counts[emotion] = int(counts.get(emotion, 0)) + 1
    profile["last_emotion"] = emotion
    if turn:
        profile["last_turn"] = turn

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return profile
