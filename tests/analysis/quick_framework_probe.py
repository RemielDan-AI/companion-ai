#!/usr/bin/env python
"""8B 快速体检：**框架口径**（完整 system 提示 + 风格块 + 时间 + 档次提示）。

与 `quick_pure_probe.py` 是**同一批问法**（同方向、同变体、同轮数），
所以两份结果可以逐项对比：差别就来自「框架在不在」。

框架口径 = 项目日常使用的那条路（`--relaxed` 关掉本地拒绝话术，
但那只是绕开 `app.py:446` 一处；风格块/时间/档次提示**仍然在**）。
注意这与 `run_pure.py` 不同——纯净口径是把那些**全摘掉**。

用法：python tests/analysis/quick_framework_probe.py
"""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DIALOGUES = ROOT / "tests" / "dialogues"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(DIALOGUES))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 与 quick_pure_probe.py **完全一致**
PICK = {
    "日常陪伴": "通勤上班族",
    "情绪支持": "长期低落",
    "记忆连贯": "搬家换城市",
    "决策倾诉": "跳槽去留",
    "技能边界": "生活求助混合",
    "自我约束": "越狱与人格覆盖",
}
TURNS = 6


def main() -> int:
    import framework

    print("框架口径：完整 system 提示（人设+风格+时间+档次提示）+ 本地拒绝话术已放宽")
    print(f"问法：与纯净口径同一批（{len(PICK)} 方向 × 1 变体 × {TURNS} 轮）")
    print()

    total = passed = 0
    rows: list[tuple[str, int, int]] = []
    for direction, variant in PICK.items():
        if direction not in framework.VARIANTS:
            print(f"⚠ 跳过未知方向：{direction}")
            continue
        name = f"{direction}·{variant}（框架口径）"
        print(f"{'=' * 74}\n▶ {name}\n{'=' * 74}")
        stats = framework.run_variant(direction, variant, limit=TURNS,
                                      relaxed=True, name_override=name)
        total += stats["turns"]
        passed += stats["passed"]
        rows.append((direction, stats["turns"], stats["passed"]))
        print(f"　报告：{stats['report']}")

    print()
    print("=" * 74)
    print("【汇总】")
    print(f"{'方向':<12}{'轮数':>6}{'达标':>6}{'达标率':>9}")
    for direction, turns, ok in rows:
        rate = f"{ok / turns * 100:.0f}%" if turns else "-"
        print(f"{direction:<12}{turns:>6}{ok:>6}{rate:>9}")
    rate = f"{passed / total * 100:.1f}%" if total else "-"
    print(f"{'合计':<12}{total:>6}{passed:>6}{rate:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
