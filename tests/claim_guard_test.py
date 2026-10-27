"""钉住「剪掉编造的旧事指涉」——`engine/style.py: strip_claims()`。

## 为什么要有这个测试

实测（`tests/analysis/fabrication_probe.py`，走 `Companion.say()`）：

    修前：A 组（无凭据）编造 **6/24 = 25%**，6 条全是「你**上次也**…」
    只补 CLAIM_MARKS + 重生成：仍 **5/24 = 21%**   ← 光靠提示词/重试治不住
    加确定性剪句后：**0/24 = 0%**，且 B 组（"我以前住哪"该说之前）保持 3/3

## 两条必须同时守住

    ① **该剪的剪掉**（含断言式旧事指涉的那一小句）
    ② **不该剪的别动**（正常回复一个字都不能改；全剪光时给安全兜底）

第 ② 条是这套东西最容易翻车的地方——「不确定的不要说得像确定的」当初
就是把正常回复也一起劝退了，净亏 8 个点。

不连模型、不起服务、很快。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.style import strip_claims  # noqa: E402

MARKS = ("你说过", "你提过", "你之前说", "你上次说",
         "上次也", "上回也", "以前也", "之前也",
         "上次说", "上次提", "你还说", "那次你", "你那次")

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 84)
    print("剪掉编造的旧事指涉　engine/style.py: strip_claims")
    print("=" * 84)

    # ① 该剪的：把带标记那一小句去掉，其余保留
    cut_cases = [
        ("你上次也吵了。这次是因为什么？", "这次是因为什么？"),
        ("嗯，我在听。你上次也提过这事。要不要说说？", "嗯，我在听。要不要说说？"),
        ("你提过这事。现在呢？", "现在呢？"),
    ]
    for src, want in cut_cases:
        got, hit = strip_claims(src, MARKS)
        check(hit and want in got, f"剪掉编造句：{src[:18]}…", f"→ {got}")

    # ② 不该剪的：正常回复一个字不动
    keep = [
        "嗯，听出来了。想说什么就说。",
        "先歇着吧，别急着动。",
        "这个我记不太清了，你再跟我说说？",
    ]
    for src in keep:
        got, hit = strip_claims(src, MARKS)
        check((not hit) and got == src, f"正常回复不动：{src[:18]}…")

    # ③ 全剪光 → 安全兜底（不能返回空串）
    got, hit = strip_claims("你上次也这么说的。", MARKS)
    # ⚠ 兜底句现在是**从数组里随机取一句**（config/replies.yaml），
    #   不能断言某个固定子串——原来写 `"记不" in got`，
    #   但新变体里有两句不含「记不」，会 2/3 概率误报。
    #   正确断言：它是候选集合里的**某一个**，且非空。
    from engine import replies as _R
    _pool = _R.all_of("no_memory")
    check(hit and got in _pool, "全剪光给安全兜底（候选之一）", f"→ {got}")

    # ④ **不误伤「以前」类正确答案**（B 组那种）
    #    注意这里传的是**没有 以前也/之前也 这些断言标记**的情形
    legit = "你以前住南京，后来搬到成都了。"
    got, hit = strip_claims(legit, ("上次也", "上回也", "你说过"))
    check((not hit) and got == legit,
          "**不误伤「以前住哪」这类正确答案**", f"→ {got}")

    # ⑤ 空标记表 = 什么都不做（防配置漏传时把回复剪空）
    got, hit = strip_claims(keep[0], ())
    check((not hit) and got == keep[0], "没传标记时不动任何东西")

    print()
    print("=" * 84)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
