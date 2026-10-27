"""钉住「主体校验」：抽取不能把别人的事记成用户自己的。

## 为什么需要这道闸门

实测（2026-10-16）：用户说「小雨是广州大学的学生」，
抽取给出 `用户是广州大学学生` ——**主语被换成了用户**。
这种错**比抽不到更糟**：它污染画像，而且多跳会跳到错的人身上
（问「小雨是哪个学校的」会问到用户头上）。

## 判据的两次返工（都记在这里，免得以后又改回去）

    ① 只用"分句开头是不是第三方" → 漏掉「我妹妹在公司上班」
       （「我」是**领属**，真正的主语是妹妹）
    ② 改成"分句覆盖了内容"+**全集**判据 → 被跨界二元组卡住
       （`大学学生` 会产生分句里没有的 `学学`）
    ③ 改成"重叠率 ≥ 阈值" → 被**改写**绕过
       （抽取写「用户**就读于**广州大学」，只重合 0.5）
    ④ 最终：**比较**——覆盖这条内容的那个分句（重叠最高）主语是别人 →
       拒写；且**必须严格高于**"用户自己"分句的重叠率
       （否则一句里同时有第三方和自己、重叠并列时会误杀）

不连模型、不起服务。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.extract import _subject_ok               # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


# (内容, 用户原话, 本轮 relations, 说明)
SHOULD_REJECT = [
    ("用户是广州大学学生", "小雨是广州大学的学生",
     ["对方提到的人：小雨（学生）"], "分句以第三方开头"),
    ("用户就读于广州大学", "小雨是广州大学的学生",
     ["对方提到的人：小雨（学生）"], "**改写**（就读于）也要拦住"),
    ("对方三岁了", "团子三岁了",
     ["对方提到的人：团子（宠物）"], "第三方开头"),
    ("用户在公司上班", "我妹妹在公司上班",
     ["对方提到的人：妹妹（家人）"], "「我」是领属，主语是妹妹"),
    ("对方对花生过敏", "我妹妹对花生过敏",
     ["对方提到的人：妹妹（家人）"], "同上"),
]

SHOULD_ALLOW = [
    ("对方妹妹叫林小雨", "我妹妹叫林小雨",
     ["对方提到的人：林小雨（妹妹）"], "内容自己带了主体"),
    ("对方妹妹在广州上学", "我妹妹在广州上学",
     ["对方提到的人：妹妹（广州上学）"], "同上"),
    ("对方在一家做工业软件的公司上班", "我在一家做工业软件的公司上班",
     [], "纯第一人称"),
    ("对方学习吉他", "我最近在学吉他", [], "纯第一人称"),
    ("对方今天很累", "我今天很累", [], "纯第一人称"),
    ("对方对花生过敏", "我对花生过敏", [], "纯第一人称"),
    ("团子三岁了", "团子三岁了",
     ["对方提到的人：团子（宠物）"], "主体不是用户"),
    ("对方住在南京", "我住在南京", [], "纯第一人称"),
    ("对方最近很累", "我妹妹叫林小雨，我最近很累",
     ["对方提到的人：林小雨（妹妹）"], "一句里既有第三方又有自己"),
    ("对方喜欢喝美式", "我妹妹喜欢喝茶，我爱喝美式",
     ["对方提到的人：妹妹（家人）"], "**重叠并列时不能误杀**"),
    ("对方学习吉他", "我妹妹也学吉他，我在学吉他",
     ["对方提到的人：妹妹（家人）"], "看似像、其实主语是自己那句"),
]


def main() -> int:
    print("=" * 80)
    print("① 该拦的：第三方的事被记成用户自己的（比抽不到更糟）")
    print("=" * 80)
    for content, user, rels, why in SHOULD_REJECT:
        got = _subject_ok(content, user, rels)
        check(got is False, why, f"「{user}」+「{content}」→ {got}")

    print()
    print("=" * 80)
    print("② 不该拦的：**误杀就是丢记忆**（宁可少拦也不误杀）")
    print("=" * 80)
    for content, user, rels, why in SHOULD_ALLOW:
        got = _subject_ok(content, user, rels)
        check(got is True, why, f"「{user}」+「{content}」→ {got}")

    print()
    print("=" * 80)
    print("③ 内容主体不是「用户/对方」时不归这道闸门管（别的闸门负责）")
    print("=" * 80)
    for content in ("团子三岁了", "林小雨在读大学", "工业软件公司（单位）",
                    "对方提到的人：林小雨（妹妹）"):
        got = _subject_ok(content, "小雨是广州大学的学生", [])
        check(got is True, f"「{content}」放行（主体不是用户/对方）", str(got))

    print()
    print("=" * 80)
    print("④ 接线：入库时真的调了这道闸门（防以后被摘掉）")
    print("=" * 80)
    src = (ROOT / "cognition" / "extract.py").read_text(encoding="utf-8")
    check(src.count("_subject_ok(") >= 2,
          "remember_turn 里真的调了 _subject_ok（函数定义 + 调用点）",
          f"出现 {src.count('_subject_ok(')} 次")
    check("跳过主体错位" in src, "被拦时会写审计日志")
    # ⚠ 关系条目必须豁免：第一版忘了，把多跳的锚点条目全拦掉了
    check('kind != "relation" and not _subject_ok(' in src.replace("\n", " ")
          or 'kind != "relation" and not _subject_ok' in src,
          "**关系条目被豁免**（关系不是「关于用户的断言」）")
    # ⚠ `_is_bare_entity` 不许再拿去做"要不要存"的闸门（实测会整条丢事实）
    bare_lines = [ln.strip() for ln in src.splitlines()
                  if "_is_bare_entity(" in ln and not ln.strip().startswith("def ")]
    check(all("_canonical" in ln or "if _is_bare_entity(text)" in ln
              for ln in bare_lines),
          "`_is_bare_entity` 只服务 `_canonical`（不再当存储闸门）",
          str(bare_lines))

    print()
    print("=" * 80)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

