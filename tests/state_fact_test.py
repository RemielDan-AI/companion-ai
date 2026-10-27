"""钉住「状态句被误归成关系／宾语被吞」的两条修复。

## 问题（实测，Qwen3.5-2B 抽取器）

能力上限探针里**单跳基线「我妹妹对什么过敏」三次全败**，查库发现：

    [relation] 对方提到的人：林小雨（过敏）      ← 过敏原「花生」没了

抽取把**状态**当成了**关系**。关系该是**角色**（妹妹/同事/宠物/单位），
「过敏」是状态——状态句是**事实**。它有两种形态，各自要一条补丁：

    ① `relations=['林小雨（过敏对象）']`
       → `_reclassify_state_relations`：状态词当关系词的，从原话捞那个分句当事实
    ② `facts=['林小雨过敏']` ＋ `relations=['林小雨']`
       → `_complete_state_facts`：**没有状态词关系可当线索**，这条路抓不到①，
         只能看事实本身"带状态词却没有宾语"

两条**都从用户原话里捞分句**（用原话，不引入编造风险）。

## 判据上的三个坑（都真踩过）

    · `_ROLE_HINTS` 里放了「对象」→「过敏**对象**」被当成角色 → 还原不出来
    · `strip("对方用户")` 会把整串「用户」剥光（strip 收的是**字符集**不是子串）
    · `pre` 里含「对」就判"已经有宾语" →「**对方**过敏」被直接跳过
      （顺序必须是：**先剥主体前缀，再查宾语标志**）

不连模型、不起服务。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.extract import (_complete_state_facts,  # noqa: E402
                               _is_state_word,
                               _reclassify_state_relations)

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 82)
    print("① 状态词判定：状态要认出来，**角色要放行**")
    print("=" * 82)
    for word, want, why in (
        ("过敏对象", True, "状态"),
        ("过敏", True, "状态"),
        ("怕狗", True, "状态"),
        ("住在", True, "状态"),
        ("喜欢", True, "状态"),
        # ⚠ 下面这些含状态词但其实是**角色**，放行错了会把好的关系拆掉
        ("学校", False, "角色（含「学」）"),
        ("上学的地方", False, "角色"),
        ("上班的地方", False, "角色"),
        ("妹妹", False, "角色"),
        ("宠物", False, "角色"),
        ("单位", False, "角色"),
    ):
        got = _is_state_word(word)
        check(got is want, why, f"{word!r} → {got}")

    print()
    print("=" * 82)
    print("② 补丁一：状态词当关系词 → 从原话还原成事实")
    print("=" * 82)
    kept, rec = _reclassify_state_relations(["林小雨（过敏对象）"],
                                            "林小雨对花生过敏")
    check(rec == ["林小雨对花生过敏"], "**宾语从原话捞回来**", str(rec))
    check(kept == [], "该关系不再保留（它本来就不是关系）", str(kept))
    kept2, rec2 = _reclassify_state_relations(["妹妹（怕狗）"], "我妹妹怕狗")
    check(rec2 == ["我妹妹怕狗"], "状态当关系也还原", str(rec2))
    kept3, rec3 = _reclassify_state_relations(["妹妹（用户提到的家人）"],
                                              "我妹妹对海鲜过敏")
    check(kept3 and not rec3, "**正常角色关系放过**", f"{kept3} {rec3}")
    kept4, rec4 = _reclassify_state_relations(["广州（学校）"],
                                              "我妹妹在广州上学")
    check(kept4 and not rec4, "**角色类型放过**（学校）", f"{kept4} {rec4}")

    print()
    print("=" * 82)
    print("③ 补丁二：事实带状态词却没宾语 → 从原话补全（补丁一抓不到的那条路）")
    print("=" * 82)
    for facts, text, want, why in (
        (["林小雨过敏"], "林小雨对花生过敏", ["林小雨对花生过敏"], "宾语被吞"),
        (["用户过敏"], "我对花生过敏", ["我对花生过敏"], "主语是用户"),
        (["对方过敏"], "我对花生过敏", ["我对花生过敏"],
         "**「对方」含「对」的坑**"),
        (["过敏"], "我对花生过敏", ["我对花生过敏"], "连主体都没有"),
        # 下面这些**不该动**
        (["妹妹对海鲜过敏"], "我妹妹对海鲜过敏", ["妹妹对海鲜过敏"], "已经有宾语"),
        (["妹妹不吃香菜"], "我妹妹不吃香菜", ["妹妹不吃香菜"], "已经有宾语"),
        (["妹妹喜欢喝美式"], "我妹妹喜欢喝美式", ["妹妹喜欢喝美式"],
         "**宾语在状态词后面**，本来就不缺"),
        (["住在南京"], "我住在南京", ["住在南京"], "有「在」"),
    ):
        out, _filled = _complete_state_facts(facts, text)
        check(out == want, why, f"{facts} → {out}")

    print()
    print("=" * 82)
    print("④ 接线：两步都在 remember_turn 里跑（防以后被摘掉）")
    print("=" * 82)
    src = (ROOT / "cognition" / "extract.py").read_text(encoding="utf-8")
    check("_reclassify_state_relations(" in src, "调了补丁一")
    check("_complete_state_facts(" in src, "调了补丁二")
    check(src.count("_reclassify_state_relations(") >= 2
          and src.count("_complete_state_facts(") >= 2,
          "都是「定义 + 调用」两处")
    check("状态句缺宾语" in src and "误归成关系" in src, "两处都写审计日志")

    print()
    print("=" * 82)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
