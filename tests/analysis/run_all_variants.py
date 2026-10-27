# -*- coding: utf-8 -*-
"""全量变体测试（18 个变体），两个口径都跑，报告带口径后缀。

为什么要单独写：`run_dialogues.py` 没有 `name_override`，
两个口径的报告会同名混在一起、没法对比（`run_pure.py` 是自己加的 name_override）。

口径定义：
    框架口径 = 项目日常使用那条路（完整 system 提示 + 风格块 + 时间 + 档次提示），
               但放宽本地拒绝话术（`rules_relaxed=True`，等价 `--relaxed`）。
    纯净口径 = `run_pure.py` 那套：system 提示**只留**「开头人设 + 记忆事实」，
               风格块/时间/档次提示/本地拒绝模板全摘。

规模提醒：18 变体 × `--limit N` 轮。默认 20 轮 → 360 轮/口径。
8B 上 8B 约 1.8 小时/口径（~18s/轮），所以两个口径要分两次跑。

用法：
    python tests/analysis/run_all_variants.py --mode framework --limit 20
    python tests/analysis/run_all_variants.py --mode pure      --limit 20
    python tests/analysis/run_all_variants.py --mode framework --limit 20 --dry-run
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DIALOGUES = ROOT / "tests" / "dialogues"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(DIALOGUES))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUFFIX = {"framework": "框架口径", "pure": "纯净口径"}


def suffix_for(mode: str, model_tag: str) -> str:
    """报告名后缀：**带上模型标签**，否则不同模型的同名报告会混在一起没法比。

    2026-10-15：8B 与 v1.3 要跑同一套变体，而 `run_variant` 的 name_override
    只进文件名——所以显式把模型写进去（如「v1.3框架口径」「8B纯净口径」）。
    """
    body = f"{model_tag}{SUFFIX[mode]}" if model_tag else SUFFIX[mode]
    return f"（{body}）"


def install_pure() -> None:
    """纯净口径：与 tests/dialogues/run_pure.py 同一套做法。"""
    from engine.generate import Generator, build_persona_block
    from tests import conv_harness

    def pure_system_prompt(self, level=None, memory_block="", extra_prompt="",
                           tail_prompt=""):
        parts = [build_persona_block(self.persona, self.persona_hints)]
        if memory_block:
            parts.append(str(memory_block).strip())
        return "\n".join(parts)

    Generator.system_prompt = pure_system_prompt


def main() -> int:
    ap = argparse.ArgumentParser()
    # 默认 framework。为什么同时支持环境变量：
    # `model_server.py --script` **不支持给脚本传参**（实测踩过两次），
    # 所以跑纯净口径时用 `$env:VARIANTS_MODE='pure'` 来切。
    ap.add_argument("--mode", choices=("framework", "pure"),
                    default=os.environ.get("VARIANTS_MODE", "framework"))
    ap.add_argument("--limit", type=int,
                    default=int(os.environ.get("VARIANTS_LIMIT", "20")),
                    help="每个变体跑前 N 轮")
    ap.add_argument("--dry-run", action="store_true", help="只列出要跑什么")
    # 模型标签：只进报告文件名，用来区分不同模型的同一套变体
    # （模型名进不了文件名的话，8B 与 v1.3 的报告会同名混在一起）
    ap.add_argument("--tag", default=os.environ.get("VARIANTS_TAG", ""))
    args = ap.parse_args()

    import framework
    jobs = [(d, v) for d in framework.directions() for v in framework.variants(d)]
    total_turns = sum(len(framework.build(d, v)) for d, v in jobs)
    picked = sum(min(args.limit, len(framework.build(d, v))) for d, v in jobs)

    sfx = suffix_for(args.mode, args.tag)
    print(f"口径：{args.mode}　模型标签：{args.tag or '（无）'}　报告后缀：{sfx}")
    print(f"变体：{len(jobs)} 个　全量 {total_turns} 轮 → 本次每个取前 {args.limit} 轮"
          f" = **{picked} 轮**")
    print(f"预计：约 {picked * 16 / 60:.0f}–{picked * 20 / 60:.0f} 分钟（8B，~16–20s/轮）")
    print()
    if args.dry_run:
        for d, v in jobs:
            print(f"  {d}·{v}　{min(args.limit, len(framework.build(d, v)))} 轮")
        return 0

    if args.mode == "pure":
        install_pure()
    # 两个口径都放宽本地拒绝话术：否则量到的是"项目规则层"而不是模型
    # （框架口径下风格块/时间/档次提示仍在，纯净口径下全摘）

    total = passed = 0
    rows: list[tuple[str, int, int]] = []
    for i, (direction, variant) in enumerate(jobs, 1):
        name = f"{direction}·{variant}{sfx}"
        print(f"\n{'=' * 74}\n▶ [{i}/{len(jobs)}] {name}\n{'=' * 74}")
        try:
            stats = framework.run_variant(direction, variant, limit=args.limit,
                                          relaxed=True, name_override=name)
        except Exception as exc:
            print(f"  ✗ 这个变体挂了：{type(exc).__name__}: {exc}")
            continue
        total += stats["turns"]
        passed += stats["passed"]
        rows.append((f"{direction}·{variant}", stats["turns"], stats["passed"]))
        print(f"　报告：{stats['report']}")

    print()
    print("=" * 74)
    print(f"【汇总·{args.mode}】")
    print(f"{'变体':<26}{'轮数':>6}{'达标':>6}{'达标率':>9}")
    for name, turns, ok in rows:
        rate = f"{ok / turns * 100:.0f}%" if turns else "-"
        print(f"{name:<26}{turns:>6}{ok:>6}{rate:>9}")
    rate = f"{passed / total * 100:.1f}%" if total else "-"
    print(f"{'合计':<26}{total:>6}{passed:>6}{rate:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
