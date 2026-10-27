"""决定性的那个对比：**过完闸门之后，① 和 ② 谁剩得多、谁剩得脏**。

## 为什么还要单独量这一条

上一个对比（`split_vs_union_probe.py`，n=60）的结果是：

    ① 只拆两次    主体 59/60　值 60/60　字段 51/60　**编造 5**
    ② 只并集两次  主体 56/60　值 55/60　字段 49/60　**编造 2**

① 在质量上全面更好，但**编造多 3 条**。而"编造进库"比"漏抽"更糟
（漏抽是没记住，编造是**记错了**）——所以不能只看原始输出。

但抽取的原始输出**不等于入库**：`remember_turn` 后面有四道闸门
（`_grounded` 有没有出处 / `_absolute_ok` 有没有被放大 /
`_subject_ok` 主体对不对 / `_strip_invented_name` 编造的名字降级）。

**所以要量的是：过完闸门之后，两个方法各自剩下多少真信息、漏掉多少编造。**
真正入库的才是系统用的。

用法：
    python tests/model_server.py run --model Qwen3.5-2B --ngl 99 \
        --script tests/analysis/split_guard_probe.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

REPEAT = max(1, int(os.environ.get("GUARD_REPEAT", "4")))

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


def _survive(info: dict, user_text: str) -> tuple[list[str], list[str]]:
    """把抽取结果逐条过**和 remember_turn 一样的四道闸门**。

    返回 (留下来的, 被闸门挡掉的)。
    """
    kept, blocked = [], []
    rels = [str(r) for r in (info.get("relations") or [])]
    for key in ("facts", "preferences", "relations"):
        kind = {"facts": "fact", "preferences": "preference",
                "relations": "relation"}[key]
        for item in (info.get(key) or []):
            content = EX._canonical(kind, str(item))
            if kind == "relation":
                content = EX._canonical(kind, EX._enrich_relation(
                    content, user_text) if hasattr(EX, "_enrich_relation") else content)
            reason = ""
            if not EX._grounded(content, user_text):
                reason = "无出处"
            elif not EX._absolute_ok(content, user_text):
                reason = "被放大"
            elif kind != "relation" and not EX._subject_ok(
                    content, user_text, rels):
                reason = "主体错位"
            if reason:
                blocked.append(f"{content}（{reason}）")
            else:
                kept.append(content)
    return kept, blocked


def _parse(gen, template, user, reply):
    raw = ""
    try:
        raw = gen.complete(EX.EXTRACT_SYSTEM, template.format(user=user, reply=reply),
                           max_tokens=400, temperature=0.2)
    except Exception:
        pass
    return EX.parse_json(raw) or {}


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"过闸门后的对比　模型：{os.environ.get('EXTRACT_MODEL_TAG') or url}"
          f"　每格 {REPEAT} 轮")
    print("=" * 100)

    stat = {"① 只拆两次": {"keep": 0, "block": 0, "sub": 0, "val": 0, "n": 0},
            "② 只并集两次": {"keep": 0, "block": 0, "sub": 0, "val": 0, "n": 0}}
    for user, subject, value in GOLD:
        for _ in range(REPEAT):
            # ① 拆：facts/relations 那半
            a = _parse(gen, TEMPLATE_A, user, "嗯")
            # ② 并集：整套跑两遍取并集
            xs = [EX.extract_turn(gen, user, "嗯") for _ in range(2)]
            u = dict(xs[0])
            for k in ("facts", "relations"):
                seen, merged = set(), []
                for it in list(xs[0].get(k) or []) + list(xs[1].get(k) or []):
                    if str(it) not in seen:
                        seen.add(str(it)); merged.append(str(it))
                u[k] = merged
            for name, info in (("① 只拆两次", a), ("② 只并集两次", u)):
                kept, blocked = _survive(info, user)
                s = stat[name]
                s["n"] += 1
                s["keep"] += len(kept)
                s["block"] += len(blocked)
                blob = " ".join(kept)
                s["sub"] += bool(subject and subject in blob)
                s["val"] += (bool(value and value in blob) if value else 1)

    print(f"{'方法':<16}{'每条留':>8}{'每条挡':>8}{'主体(过闸后)':>14}"
          f"{'值(过闸后)':>13}")
    print("-" * 100)
    n = stat["① 只拆两次"]["n"]
    for name, s in stat.items():
        print(f"{name:<16}{s['keep']/s['n']:>8.2f}{s['block']/s['n']:>8.2f}"
              f"{s['sub']:>10}/{n:<3}{s['val']:>9}/{n:<3}")
    print()
    print("读法：")
    print("  · 「每条留」高 = 每条原话留下更多真信息（召回）")
    print("  · 「每条挡」高 = 抽出来的东西被闸门挡掉的多（**编造多**）")
    print("  · 主体/值 都是**过完闸门**之后再算的——这才是系统真拿到的东西")
    print()
    print('⚠ 判据是「留下更多真信息」+「挡掉的别更多」；')
    print("   若 ① 留下更多、挡掉也没多多少 → **① 值得换**；")
    print("   若 ① 挡掉明显更多 → 编造多，换过去是拿「记错」换「多记」，不值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

