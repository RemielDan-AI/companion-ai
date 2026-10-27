"""钉住关系抽取的两处修正（都不调模型）。

## 背景（2026-10-15 实测发现）

一拍脑袋以为「抽取会产出 `名字（关系）`」——真的跑抽取器才发现**从来不产出**：

    用户「我妹妹叫林小雨」      → relations = ['林小雨']        （只有名字）
    用户「我妹妹在读大学」      → relations = ['妹妹']          （只有关系词）
    用户「我在一家做软件的公司上班」→ relations = []              （机构不进 relations）

而一跳扩展（`engine/retrieve.py: relation_targets`）认的是 `名字（关系）`
（要拿括号里的关系词去匹配查询）。所以**锚点链路实际从没接通过**，
先前的多跳成绩是手写理想数据跑出来的假象。

两处修正：
    ① `_enrich_relation` **双向绑定**（原来只做「关系词 + 叫XX」一个方向）
    ② 提示词明确要求 `名字（关系）`，并把机构/宠物/地点纳入 relations

## 顺带发现的安全问题（也在这里钉住）

改完提示词后，用户只说「我妹妹在读大学」（**整句没有名字**），
模型输出「**林小雨**（妹妹）」——`林小雨` 在微调数据里出现太多次，模型把它**补全**了。
`_grounded()` 拦不住：body 里的「妹妹」在原话里有，**接地部分掩护了编造部分**。
所以加了 `_strip_invented_name`：名字必须自己有出处，否则降级只留关系词。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 76)
    print("① 双向绑定：模型给名字或给关系词，都要落成「名字（关系）」")
    print("=" * 76)
    cases = [
        ("林小雨", "我妹妹叫林小雨", "林小雨（妹妹）", "反向：模型给名字"),
        ("妹妹", "我妹妹叫林小雨", "林小雨（妹妹）", "正向：模型给关系词"),
        ("团子", "我养了只猫叫团子", None, "反向：宠物"),
        ("林小雨（妹妹）", "我妹妹叫林小雨", "林小雨（妹妹）",
         "已是目标形态 → 不叠加"),
        ("妹妹", "我妹妹在读大学", "妹妹", "没有名字 → 保持原样，**不硬编**"),
        ("北京", "我上个月从南京搬到了北京", "北京",
         "名字但附近没关系词 → 不硬绑"),
    ]
    for body, text, expect, why in cases:
        got = EX._enrich_relation(body, text)
        if expect is not None:
            check(got == expect, why, f"{body!r}→{got!r}")
        else:
            check("（" in got and "）" in got, why, f"{body!r}→{got!r}")

    print()
    print("=" * 76)
    print("② 接地校验：**编造的名字**要降级（`_grounded` 拦不住这种）")
    print("=" * 76)
    guard = [
        ("林小雨（妹妹）", "我妹妹叫林小雨", True, "真名字（原话有）"),
        ("林小雨（妹妹）", "我妹妹在读大学", False,
         "**编造**（原话无名字）← 实测模型真会这么吐"),
        ("团子（宠物）", "我养了只猫叫团子", True, "真名字"),
        ("工业软件公司（上班的地方）", "我在一家做工业软件的公司上班", True,
         "机构名（二字片段命中即可）"),
        ("妹妹", "我妹妹在读大学", True, "没有括号 → 不适用"),
    ]
    for content, text, keep, why in guard:
        got = EX._strip_invented_name(content, text)
        check((got == content) == keep, why,
              f"{content!r}→{got!r}")

    # 降级后不能把有用信息也砍掉
    got = EX._strip_invented_name("林小雨（妹妹）", "我妹妹在读大学")
    check(got == "妹妹", "降级只砍编造的名字，保留关系词（不整条丢）", f"{got!r}")

    print()
    print("=" * 76)
    print("③ 提示词确实要求了目标形态（静态检查，防以后被改回去）")
    print("=" * 76)
    tpl = EX.EXTRACT_TEMPLATE
    check("名字（关系）" in tpl, "提示词写了「名字（关系）」")
    check("林小雨（妹妹）" in tpl, "给了人名示例")
    check("团子（宠物）" in tpl, "给了宠物示例")
    check("工业软件公司（上班的地方）" in tpl, "给了**机构**示例（原来只有人名）")
    check("不要只收人名" in tpl, "明确说了机构/地点也要收")

    print()
    print("=" * 76)
    print("④ 接地校验接在落库链路上（静态检查）")
    print("=" * 76)
    src = (ROOT / "cognition" / "extract.py").read_text(encoding="utf-8")
    check("_strip_invented_name(" in src.split("def remember_turn")[-1],
          "remember_turn 里调用了 _strip_invented_name")
    check("_enrich_relation(" in src.split("def remember_turn")[-1],
          "remember_turn 里调用了 _enrich_relation")

    print()
    print("=" * 76)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
