"""钉住「编属性」的确定性防线：`cognition/verify.py: unsupported_numbers()`。

## 它修的是什么

    问「今年是哪一年」
    → 「2026 年——**你还没满 18 周岁呢**」

证据里只有年份和时分，「18」哪儿都没有 —— 这是**凭空断言用户属性**。
和"编前情"（「你上次也…」，由 `strip_claims` 管）不是一类，那条守卫抓不到。

这类编造**会随功能变多而变多**（每个新技能/数据源都是一个能编的面），
所以要有确定性防线，而不是只靠提示词。

## 两条必须同时守住

    ① **该抓的抓到**（回复里出现证据没有的数字）
    ② **不该抓的别抓** —— 尤其是：
        · 用户自己说过的数字（他说「走了 8000 步」，回复带 8000 是对的）
        · 时间数字（20:30 是程序给的，不在记忆块里）
        · 「一个人」「这个」「那个」这类**不是数量**
    第 ② 条是重点：**误报比漏报更糟**——会去改一句本来对的话。

不连模型、不起服务、很快。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.verify import numbers_in, unsupported_numbers  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 88)
    print("编属性防线：unsupported_numbers")
    print("=" * 88)

    # ---- ① 该抓的 ----
    caught = [
        ("现在：2026-10-21 20:28（星期三）", "2026 年——你还没满 18 周岁呢", "18"),
        ("对方妹妹林小雨对花生过敏", "你妹妹三岁吧？", "3"),
        ("", "你有两个孩子吧", "2"),
        ("", "三份工作挺累的吧", "3"),
    ]
    for ev, rp, want in caught:
        got = unsupported_numbers(ev, rp)
        check(want in got, f"抓到编造的数字：{rp[:20]}…", f"→ {got}")

    # ---- ② 不该抓的（误报比漏报更糟）----
    keep = [
        # 用户自己说过的数字
        ("我今天走了 8000 步", "8000 步挺多的，腿该酸了吧", "用户说过的数字"),
        # 时间数字：程序给的，不在记忆块里
        ("现在：2026-10-21 20:30（星期三）", "现在 20:30 了，该歇了", "时间数字"),
        # 「一个人」「这个」不是数量
        ("", "一个人待着也挺好", "「一个人」不算数量"),
        ("", "这个我不太懂", "「这个」不算数量"),
        # 有凭据的数字
        ("对方妹妹林小雨三岁", "你妹妹三岁了", "有凭据的三岁"),
        # 中文数字与阿拉伯数字要能对上
        ("对方妹妹三岁", "你妹妹 3 岁了", "中文/阿拉伯能对上"),
    ]
    for ev, rp, why in keep:
        got = unsupported_numbers(ev, rp)
        check(not got, f"**不该抓**：{why}", f"→ {got}")

    # ---- ③ 抽数字本身 ----
    check("20:16" in numbers_in("20:16 周三"), "时刻要整段抽（20:16 不是 20）")
    check("8000" in numbers_in("走了 8000 步"), "阿拉伯数字")
    check("18" in numbers_in("十八岁"), "「十八」→ 18")
    check("3" in numbers_in("三岁"), "「三」→ 3")

    # ---- ④ 空输入不炸 ----
    check(unsupported_numbers("", "") == [], "空输入返回空表")
    check(unsupported_numbers("任意", "") == [], "空回复返回空表")

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
