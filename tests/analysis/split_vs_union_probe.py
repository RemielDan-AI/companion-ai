"""**三种抽取策略的受控对比**：只拆两次 / 只并集两次 / 拆两次×并集两次。

## 三个方法（都用同一批金标准）

    ① split       拆两次：A 只抽 facts+relations，B 只抽 summary+emotion+preferences
                  → **2 次调用**，每个字段只有 1 份样本
    ② union       并集两次：整套提示词跑两遍、按字段取并集（**= 现在的生产行为**）
                  → **2 次调用**，每个字段有 2 份样本
    ③ split_union 拆两次 × 各并集两次
                  → **4 次调用**，每个字段有 2 份样本

## 这个对比真正要回答的两个问题

    · 同样是 **2 次调用**：**拆开**好，还是**重复一遍取并集**好？
      （① vs ② —— 这才是同预算下的公平比较）
    · 多花 2 次调用去"拆开**并且**重复"，比只重复一遍值不值？
      （③ vs ② —— 拆开本身有没有带来增益）

## 为什么拆开可能有用 / 可能没用

有用：输出变短、schema 压力小，facts/relations 那半可以专注在精确字段上。
没用：`summary` 是一次概括，拆开后它**看不到 facts 那份的输出**，
      可能和 facts 不一致；而且总调用数翻倍、延迟翻倍。

**打分只打 facts/relations 那一侧**（summary/emotion 影响小），
另记 B 侧产出的完整度。

用法：
    python tests/model_server.py run --model Qwen3.5-2B --ngl 99 \
        --script tests/analysis/split_vs_union_probe.py
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

REPEAT = max(1, int(os.environ.get("SPLIT_REPEAT", "2")))

# ---- A 半：只要 facts + relations（把无关的字段说明去掉）----
TEMPLATE_A = """用户说：{user}

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

# ---- B 半：summary + emotion + importance + preferences ----
TEMPLATE_B = """用户说：{user}

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

_STATE = ("过敏", "喜欢", "讨厌", "住在", "上班", "上学", "读书", "工作", "怕")
_PAIR = re.compile(r"(.{2,12})（(.{1,8})）")

# (原话, 主体, 值)
GOLD = [
    ("我妹妹叫林小雨", "林小雨", ""),
    ("林小雨对花生过敏", "林小雨", "花生"),
    ("我妹妹对海鲜过敏", "妹妹", "海鲜"),
    ("我妹妹不吃香菜", "妹妹", "香菜"),
    ("我住在南京", "南京", ""),
    ("我已经搬到成都了", "成都", ""),
    ("我养了只猫叫团子", "团子", ""),
    ("团子三岁了", "团子", "三岁"),
    ("我同事陈思远做后端", "陈思远", "后端"),
    ("小花是我妹妹的同学", "小花", ""),
    ("我妹妹在广州大学上学", "广州大学", "广州大学"),
    ("我妈在杭州住院了", "杭州", "住院"),
]


def _facts_blob(info: dict) -> str:
    return " ".join(str(x) for k in ("facts", "relations")
                    for x in (info.get(k) or []))


def _hit(info: dict, subject: str, value: str) -> bool:
    blob = _facts_blob(info)
    return (not subject or subject in blob) and (not value or value in blob)


def _fields_ok(info: dict) -> tuple[bool, str]:
    for r in (info.get("relations") or []):
        m = _PAIR.search(str(r))
        if not m:
            return False, f"关系形态不对：{r}"
        if any(s in m.group(2) for s in _STATE):
            return False, f"状态词当关系：{r}"
    return True, ""


def _invented(info: dict, user_text: str) -> int:
    n = 0
    for k in ("facts", "preferences"):
        for item in (info.get(k) or []):
            body = EX._canonical(k[:-1], str(item))
            for pfx in ("对方提到的人：", "对方", "用户"):
                if body.startswith(pfx):
                    body = body[len(pfx):]
                    break
            if not EX._grounded(body, user_text):
                n += 1
    return n


def _union(a: dict, b: dict) -> dict:
    out = dict(a)
    for k in ("facts", "relations", "preferences"):
        seen, merged = set(), []
        for item in list(a.get(k) or []) + list(b.get(k) or []):
            s = str(item)
            if s not in seen:
                seen.add(s)
                merged.append(s)
        out[k] = merged
    return out


def _parse(gen, system, template, user, reply):
    prompt = template.format(user=user, reply=reply)
    raw = ""
    try:
        raw = gen.complete(system, prompt, max_tokens=400, temperature=0.2)
    except Exception:
        pass
    return EX.parse_json(raw) or {}


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"三策略对比　模型：{os.environ.get('EXTRACT_MODEL_TAG') or url}"
          f"　每格 {REPEAT} 轮")
    print("=" * 104)

    # ⚠ 每个 (用例 × 方法) 的计数器必须从**零**开始。
    #   第一版写成 `dict(v) for k,v in methods.items()`——那是把**累计总数**
    #   复制了过来，于是加完再累加回去 → 越滚越大（表里出现 8190/24 这种值）。
    _KEYS = ("calls", "ok", "sub", "val", "field", "inv", "lost", "b_ok", "secs")
    methods = {
        "① 只拆两次": {k: 0 for k in _KEYS},
        "② 只并集两次": {k: 0 for k in _KEYS},
        "③ 拆×并(4次)": {k: 0 for k in _KEYS},
    }

    for user, subject, value in GOLD:
        per = {k: {kk: 0 for kk in _KEYS} for k in methods}
        for mname in methods:
            for _ in range(REPEAT):
                t0 = time.time()
                if mname.startswith("①"):
                    a = [_parse(gen, EX.EXTRACT_SYSTEM, TEMPLATE_A, user, "嗯")]
                    b = [_parse(gen, EX.EXTRACT_SYSTEM, TEMPLATE_B, user, "嗯")]
                    per[mname]["calls"] += 2
                    info = dict(a[0]); info["preferences"] = b[0].get("preferences") or []
                    bfilled = bool(b[0].get("summary"))
                elif mname.startswith("②"):
                    xs = [EX.extract_turn(gen, user, "嗯") for _ in range(2)]
                    per[mname]["calls"] += 2
                    info = _union(xs[0], xs[1])
                    bfilled = bool(xs[0].get("summary"))
                else:
                    aa = [_parse(gen, EX.EXTRACT_SYSTEM, TEMPLATE_A, user, "嗯")
                          for _ in range(2)]
                    bb = [_parse(gen, EX.EXTRACT_SYSTEM, TEMPLATE_B, user, "嗯")
                          for _ in range(2)]
                    per[mname]["calls"] += 4
                    info = _union(aa[0], aa[1])
                    info["preferences"] = _union(bb[0], bb[1]).get("preferences") or []
                    bfilled = bool(bb[0].get("summary"))
                per[mname]["secs"] += time.time() - t0
                per[mname]["sub"] += bool(subject and subject in _facts_blob(info))
                per[mname]["val"] += (bool(value and value in _facts_blob(info))
                                      if value else 1)
                fok, _r = _fields_ok(info)
                per[mname]["field"] += fok
                per[mname]["inv"] += _invented(info, user)
                per[mname]["lost"] += bool(_facts_blob(info).strip())
                per[mname]["b_ok"] += bfilled
                per[mname]["ok"] += bool(subject and subject in _facts_blob(info)
                                         and (not value or value in _facts_blob(info)))
        row = []
        for mname in methods:
            p = per[mname]
            row.append(f"{mname}: {'✅' if p['ok']==REPEAT else ('◐' if p['ok'] else '✗')}")
        print(f"  {' '.join(row)}　{user}　「{subject}」「{value}」")
        for mname in methods:
            for k in methods[mname]:
                methods[mname][k] += per[mname][k]

    print()
    print("=" * 104)
    print(f"{'方法':<16}{'调用/轮':>7}{'主体':>9}{'值':>9}{'字段':>9}"
          f"{'编造':>7}{'有条目':>8}{'B侧完整':>9}{'平均秒':>9}")
    print("-" * 104)
    n = len(GOLD) * REPEAT
    for mname, p in methods.items():
        print(f"{mname:<16}{p['calls']/n:>7.1f}"
              f"{p['sub']:>6}/{n:<2}{p['val']:>6}/{n:<2}{p['field']:>6}/{n:<2}"
              f"{p['inv']:>7}{p['lost']:>5}/{n:<2}{p['b_ok']:>6}/{n:<2}"
              f"{p['secs']/n:>9.2f}")
    print()
    print("读法：")
    print("  · ① vs ② 是**同预算（都是 2 次）**的公平比较 —— 拆开和重复一遍取并集，谁好")
    print("  · ③ vs ② 看**多花 2 次调用**去同时「拆开+重复」值不值")
    print("  · 「值」那列最能说明问题：丢宾语是抽取器最主要的毛病")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
