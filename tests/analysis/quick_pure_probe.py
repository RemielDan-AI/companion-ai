#!/usr/bin/env python
"""8B 快速体检：**纯净口径**（只给「开头人设 + 记忆事实」，不跑变体）。

为什么要单独写一个：`model_server.py --pure` 会拉起 `run_pure.py --all`
（6 方向 × 3 变体 = 18 个），那不是"快速看看表现"。
这里只取每个方向 1 个变体、每变体 6 轮，总量小、出结论快。

纯净口径 = 与 `tests/dialogues/run_pure.py` **同一套**做法：
    · `Generator.system_prompt` 换成「开头人设 + 记忆块」两项，其余全摘
      （风格块、时间、l0-l3 档次提示、本地拒绝模板）
    · `rules_relaxed = True`
所以量到的是**模型本身**，不是"项目脚手架 + 模型"的合力。

用法：python tests/analysis/quick_pure_probe.py
"""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DIALOGUES = ROOT / "tests" / "dialogues"
# 注意顺序：dialogues 要在 tests 之前，因为 framework.py 在 tests/dialogues/ 下，
# 而 run_pure.py 也是那样 import 的（照它的做法保持一致）
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(DIALOGUES))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 每个方向取一个代表变体（不跑全量）
PICK = {
    "日常陪伴": "通勤上班族",
    "情绪支持": "长期低落",
    "记忆连贯": "搬家换城市",
    "决策倾诉": "跳槽去留",
    "技能边界": "生活求助混合",
    "自我约束": "越狱与人格覆盖",
}
TURNS = 6          # 每变体轮数：够看出性格，又不会跑太久


def main() -> int:
    from engine.generate import Generator, build_persona_block
    from tests import conv_harness
    import framework

    def pure_system_prompt(self, level=None, memory_block="", extra_prompt="",
                           tail_prompt=""):
        parts = [build_persona_block(self.persona, self.persona_hints)]
        if memory_block:
            parts.append(str(memory_block).strip())
        return "\n".join(parts)

    Generator.system_prompt = pure_system_prompt

    original_init = conv_harness.Companion.__init__

    def patched_init(self, *a, **kw):
        original_init(self, *a, **kw)
        self.rules_relaxed = True

    conv_harness.Companion.__init__ = patched_init

    print("纯净口径：系统提示 = 开头人设 + 记忆事实（风格/时间/档次/拒绝模板全部摘掉）")
    print(f"问法：{len(PICK)} 个方向 × 1 个变体 × {TURNS} 轮")
    print()

    total = passed = 0
    rows: list[tuple[str, int, int, dict]] = []
    for direction, variant in PICK.items():
        if direction not in framework.VARIANTS:
            print(f"⚠ 跳过未知方向：{direction}")
            continue
        name = f"{direction}·{variant}（纯净口径）"
        print(f"{'=' * 74}\n▶ {name}\n{'=' * 74}")
        stats = framework.run_variant(direction, variant, limit=TURNS,
                                      relaxed=True, name_override=name)
        total += stats["turns"]
        passed += stats["passed"]
        rows.append((direction, stats["turns"], stats["passed"], stats))
        print(f"　报告：{stats['report']}")

    print()
    print("=" * 74)
    print("【汇总】")
    print(f"{'方向':<12}{'轮数':>6}{'达标':>6}{'达标率':>9}")
    for direction, turns, ok, _ in rows:
        rate = f"{ok / turns * 100:.0f}%" if turns else "-"
        print(f"{direction:<12}{turns:>6}{ok:>6}{rate:>9}")
    rate = f"{passed / total * 100:.1f}%" if total else "-"
    print(f"{'合计':<12}{total:>6}{passed:>6}{rate:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
