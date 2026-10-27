"""**轮次级回读校验**：拿用户原话去核对抽取结果，抓**漏抽**和**错抽**。

## 它解决什么

抽取是**单轮盲抽**（`Generator.complete` 无历史），漏抽是它的主要毛病——
实测同一句「林小雨对花生过敏」，有时抽出 `花生`、有时只留 `过敏`。
现在的兜法是**盲抽两次取并集**（8/12 → 11/12），但那有个明显浪费：
**不知道漏了什么，就只好整句重抽一遍**。

本模块先当**检测器**：把原话切成"断言单元"，逐个核对**有没有被抽到的条目覆盖**。
拿到"漏了哪几句"之后，才能做两件更值钱的事：

    ① **有目标的重抽**：只针对漏掉的那几句补一次，而不是整句重抽
    ② **算一个可观测的指标**：覆盖度——它让"这次抽得好不好"变成数字

## ⚠ 判据的难点：**不能乱报**

原话里很多话**本来就不该抽出东西**，报成"漏抽"就是**狼来了**：
    · 提问：「你觉得我该不该转学」→ 没有事实
    · 空轮：「嗯，还行吧」        → 没什么可抽
    · 寒暄：「今天好累啊」        → 是状态，但按设计不进 facts
所以只对**看起来像断言**的分句做核对（有主体/状态词/关系线索），
其余一律不报。**宁可少报，也不要把噪声当漏抽**——
这条规矩和主体校验那条一脉相承：**误杀就是丢记忆，乱报就是让人不信这个数**。
"""
from __future__ import annotations

import re

# 分句切分：中文标点 + 换行
_SPLIT = re.compile(r"[，。！？；\n\r、]+")

# 不像断言的分句：提问 / 寒暄 / 应答 / 语气词
_NOT_ASSERTION = re.compile(
    r"(吗|呢|吧|么)[？?]?$|^[嗯哦啊唉呀哈]+$|^(好的?|行|可以|没事|谢谢|哈哈+)$|"
    r"^(你|您)(觉得|认为|说|看)|[？?]$"
)

# 断言线索：出现这些，才认为"这句本该抽出点什么"
_CUES = ("叫", "是", "在", "有", "对", "不", "喜", "爱", "怕", "住", "搬",
         "上", "工", "做", "养", "过敏", "岁", "住", "吃", "喝")
# 跟"主体"一起出现才算（避免「今天好累」这种纯状态被当成漏抽）
_ABOUT_SELF = ("我", "咱")

# 虚词：一个二元组**两个字都是虚词**时丢掉（它不承载信息）
_STOP_CHARS = set("的了呢吗吧啊哦嗯我你他她它们这那是有在也就都还又和跟"
                  "对把被给从着过会很太真挺不没别要能可")


def split_clauses(text: str) -> list[str]:
    return [c.strip() for c in _SPLIT.split(str(text or "")) if c.strip()]


def _bigrams(t: str) -> set[str]:
    return {t[i:i + 2] for i in range(len(t) - 1)
            if all("\u4e00" <= ch <= "\u9fff" for ch in t[i:i + 2])}


def _has_subject(clause: str) -> bool:
    """这句有"主体"吗（判断它是不是断言的关键之一）。

    ⚠ 不能只认「我/咱」（2026-10-16 实测漏洞）：
      「**林小雨**对花生过敏」是**满名当主语、没有「我」**，
      只认「我」的话这句会被判成"不是断言"直接跳过 ——
      于是它**丢了一半**（只抽出 `林小雨过敏`、丢了 `花生`）也报不出来。
      用名字识别兜住：分句开头像名字也算有主体。
    """
    c = str(clause or "")
    if any(s in c for s in _ABOUT_SELF) or "叫" in c:
        return True
    try:
        from cognition.terms import is_name_like
    except Exception:
        return False
    return any(is_name_like(c[:n]) for n in (4, 3, 2) if len(c) >= n)


def looks_like_assertion(clause: str) -> bool:
    """这句原话**本该抽出点什么**吗？

    ⚠ 保守：认不出是断言就不核对（宁可漏报，不可狼来了）。
    """
    c = clause.strip()
    if len(c) < 4:                       # 太短，没什么可抽
        return False
    if _NOT_ASSERTION.search(c):
        return False
    if any(q in c for q in ("吗", "呢", "？", "?")):
        return False
    return any(cue in c for cue in _CUES) and _has_subject(c)


def _content_grams(text: str) -> set[str]:
    """只留**有实义**的二元组（至少一个字不是虚词）。

    ⚠ 必须滤掉虚词（2026-10-16 实测）：「我养了只猫叫团子」整句 7 个二元组里
      5 个是「我养/养了/了只/只猫/猫叫」这种虚字组合，而抽取出来的条目
      只有 `团子（宠物）`——按全二元组算覆盖率只有 1/7，
      于是**抽对了也报"漏抽"**（实测假阳性 8/12，精确率只有 20%）。
    """
    out: set[str] = set()
    t = str(text or "")
    for i in range(len(t) - 1):
        g = t[i:i + 2]
        if not all("\u4e00" <= ch <= "\u9fff" for ch in g):
            continue
        if all(ch in _STOP_CHARS for ch in g):
            continue
        out.add(g)
    return out


def coverage(user_text: str, info: dict, *, partial_below: float = 0.6) -> dict:
    """原话里的断言分句，有几成被抽到的条目覆盖。

    ## 两级判据（这是被实测逼出来的设计）

    第一版要求**单个条目**覆盖分句 50% 的二元组 → **精确率只有 20%**
    （8/12 抽对了也报漏抽）。因为抽取器**本来就会把一句话拆成几条并改写**：
        「我养了只猫叫团子」→ `团子（宠物）`
        「我妹妹在广州大学上学」→ `广州大学（学校）`
    按"单条覆盖"算，这些永远不达标。

    但也不能简单地"沾边就算覆盖"——那样**丢了一半**（只抽出 `林小雨过敏`、
    丢了 `花生`）就报不出来了，而**部分丢失才是最常见的**。

    所以分两级，**强的当闸门、弱的只当指标**：

        `uncovered`  分句的实义二元组与条目**完全不相交** = 整句没着落
                     → 精确率高，**可以拿来决定要不要重抽**
        `partial`    有交集但低于 `partial_below` = 有着落、可能丢了细节
                     → 常见的抖动，**只当指标/Warning，不触发重抽**
                      （20% 精确率的东西拿来当闸门 = 每轮都重抽，等于没省）
    """
    items = []
    for key in ("facts", "preferences", "relations"):
        items += [str(x) for x in (info.get(key) or [])]
    union: set[str] = set()
    for i in items:
        union |= _content_grams(i)

    clauses = [c for c in split_clauses(user_text) if looks_like_assertion(c)]
    covered, partial, uncovered = [], [], []
    for c in clauses:
        cg = _content_grams(c)
        if not cg:
            covered.append(c)
            continue
        hit = len(cg & union) / len(cg)
        if hit <= 0:
            uncovered.append(c)
        elif hit < partial_below:
            partial.append(c)
        else:
            covered.append(c)
    return {
        "clauses": clauses,
        "covered": covered,
        "partial": partial,
        "uncovered": uncovered,
        "ratio": round(len(covered) / len(clauses), 3) if clauses else 1.0,
        "strict_ratio": (round((len(covered) + len(partial)) / len(clauses), 3)
                         if clauses else 1.0),
    }


def should_retry(cov: dict) -> bool:
    """要不要为这一轮**再抽一次**。

    ⚠ **只认 `uncovered`**（整句没着落），不认 `partial`：
      `partial` 是抖动的常态，用 20% 精确率的信号当闸门 = 每轮都重抽 = 没省。
    """
    return bool(cov.get("uncovered"))


# ===========================================================================
# **回复侧的事实保真**：回复里的数字必须能从证据里找到
# ===========================================================================
#
# ## 它解决什么（用户提的"两道门"里的第二道）
#
# 大脑生成 → [门①] → 转述 → [门②] → 用户。两道门**不能互相替代**：
#   · 门②（转述保真）挡不住大脑自己编的——转述员会忠实转过去
#   · 门①（证据对齐）挡不住转述时加的——那是复述环节的事
#
# 所以这里做**门②的核心**：把「回复」对「证据」做一次确定性核对——
# **回复里出现的数字，必须在证据里能找到**。找不到 = 编的。
#
# 实测抓到的例子（v1.5）：
#     问「今年是哪一年」→「2026 年——**你还没满 18 周岁呢**」
#     证据里只有年份和时分，「18」哪里都没有 → 这是**凭空断言用户属性**
#
# ## ⚠ 为什么只用数字，不做通用"Ner"
#
# 中文专名识别（人名/机构）没有可靠的确定性做法，硬做必然一堆误报。
# 而**数字是最高精度的那一类**：编造的属性、年龄、金额、次数，几乎都带数字。
# 宁可只抓这一类，也不要天天误报把用户和模型都搞烦——
# **乱报就是让人不信这个数**（和本文件开头那条规矩同一个道理）。
#
# ## 不能误伤的两类（很要紧）
#
#   ① **用户原话里的数字**：他说「今天走了 8000 步」，回复里出现 8000 是对的
#      → 所以证据必须把 user_text 也算进去
#   ② **时间数字**：现在几点是程序给的（`now_line()` / 时间技能），
#      不在记忆块里，但**是合法的** → 调用方要把时间串一起传进 evidence

# 阿拉伯数字（含「20:16」「3.5」「1,200」）
_NUM = re.compile(r"\d+(?:[.,:：]\d+)*")
# 中文数字 + 量词（"十八岁""三岁""五个月"）——只收**属性味**强的量词
_CN_ATTR = re.compile(
    r"([一二三四五六七八九十两百千万]+)\s*"
    r"(岁|周岁|年了|个月|年|次|条|只|门|口人|小时|分钟|天|周|号)"
)
# 「两个孩子」「三份工作」这类也带数量，但「个」**误报风险最高**：
# 「一个人」「这个」「那个」满地都是。所以「个」单独处理，**只在数字 ≥2 时才算**
# （「一个人」跳过、「两个孩子」抓）。
_CN_GE2 = re.compile(r"([一二三四五六七八九十两]+)\s*(个|份|次|门|口)")
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(text: str) -> int | None:
    """把「十八」「二十」「三」这类中文数字转成 int（只处理百以内）。"""
    s = str(text or "").strip()
    if not s:
        return None
    if "百" in s or "千" in s or "万" in s:
        return None                      # 大数少见，不猜，宁可不报
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _CN_DIGIT.get(head, 1) if head else 1
        ones = _CN_DIGIT.get(tail, 0) if tail else 0
        return tens * 10 + ones
    if len(s) == 1:
        return _CN_DIGIT.get(s)
    return None


def numbers_in(text: str) -> set[str]:
    """抽出文本里**值得核对**的数字（阿拉伯 + 中文量词数字），统一成字符串。"""
    out: set[str] = set()
    t = str(text or "")
    for m in _NUM.finditer(t):
        out.add(m.group(0))
    for m in _CN_ATTR.finditer(t):
        n = _cn_to_int(m.group(1))
        if n is not None:
            out.add(str(n))
    for m in _CN_GE2.finditer(t):
        n = _cn_to_int(m.group(1))
        if n is not None and n >= 2:      # ⚠ 「一个人」「一个道理」跳过
            out.add(str(n))
    return out


def _to_float(tok: str) -> float | None:
    try:
        return float(str(tok).replace(",", ""))
    except Exception:
        return None


def _supported(r: str, ev: set[str]) -> bool:
    """回复里的这个数字，在证据里**有没有依据**。

    ## 为什么要容差（2026-10-21 实测踩到）

    工具查到「杭州今天晴间多云，**24.5℃**」，模型说「杭州今天晴，**24 度**上下」——
    **合法的取整**，却被严格字符串比对判成"编造"，于是重生成 + 剪句，
    整句变成了兜底话术「这个我记不太清了」。**误杀比漏报严重得多。**

    所以三种都算有依据：
      ① 一模一样
      ② **数值接近**（取整/四舍五入，容差 0.5 或 1%）
      ③ **时刻的前缀**：回复说「20」，证据有「20:30」

    ⚠ 但**不做**无条件的字符串前缀：「1」是「18」的前缀，
      那样任何一位数都能混过去。只对**冒号形式**（时刻）放前缀。
    """
    if r in ev:
        return True
    rf = _to_float(r)
    for e in ev:
        ef = _to_float(e)
        if rf is not None and ef is not None:
            if abs(rf - ef) <= max(0.5, 0.01 * abs(ef)):
                return True
        # 时刻：「20」对「20:30」——只认冒号，不做泛化前缀
        if rf is not None and re.match(r"^" + re.escape(r) + r"[:：]", e):
            return True
    return False


def unsupported_numbers(evidence: str, reply: str) -> list[str]:
    """回复里断言了、但**证据里找不到依据**的数字。

    `evidence` 要把**所有合法来源**都并进来：用户原话 + 记忆块 + 工具结果 +
    时间串。少给一样就会误报——那比漏报更糟（会去改一句本来对的话）。
    """
    ev = numbers_in(evidence)
    return sorted(n for n in numbers_in(reply) if not _supported(n, ev))
