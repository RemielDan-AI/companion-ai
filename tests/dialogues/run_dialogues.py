"""对话测试变体的统一入口。

用法：
    python tests/dialogues/run_dialogues.py --list            看有哪些方向与变体
    python tests/dialogues/run_dialogues.py --scaffold        按方向建文件夹、生成变体脚本
    python tests/dialogues/run_dialogues.py --dir 日常陪伴     跑一个方向的全部变体
    python tests/dialogues/run_dialogues.py --dir 情绪支持 --variant 失恋与被离开
    python tests/dialogues/run_dialogues.py --all             跑全部方向全部变体
    python tests/dialogues/run_dialogues.py --dir 自我约束 --relaxed
                                                             跳过本地拒绝规则跑（只看模型自己）

加 --limit N 可以先跑前 N 轮（快速看效果）。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import framework  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="对话测试变体（同一考点、多种变体）")
    parser.add_argument("--list", action="store_true", help="列出方向与变体")
    parser.add_argument("--scaffold", action="store_true", help="生成文件夹与变体脚本")
    parser.add_argument("--dir", default="", help="方向名（如 日常陪伴）")
    parser.add_argument("--variant", default="", help="变体名（不指定就跑该方向全部）")
    parser.add_argument("--all", action="store_true", help="跑全部方向")
    parser.add_argument("--relaxed", action="store_true", help="跳过本地拒绝规则跑")
    parser.add_argument("--limit", type=int, default=0, help="每个变体只跑前 N 轮")
    args = parser.parse_args()

    if args.list:
        for direction in framework.directions():
            print(f"【{direction}】{framework.describe(direction, framework.variants(direction)[0])}")
            for variant in framework.variants(direction):
                print(f"    · {variant}")
        return 0

    if args.scaffold:
        made = framework.write_scaffold()
        print(f"已生成 {len(made)} 个文件：")
        for path in made:
            print("   " + str(path.relative_to(pathlib.Path(__file__).resolve().parent)))
        return 0

    jobs: list[tuple[str, str]] = []
    if args.all:
        jobs = [(d, v) for d in framework.directions() for v in framework.variants(d)]
    elif args.dir:
        if args.dir not in framework.VARIANTS:
            print(f"没有这个方向：{args.dir}（可用：{'、'.join(framework.directions())}）")
            return 1
        chosen = [args.variant] if args.variant else framework.variants(args.dir)
        jobs = [(args.dir, v) for v in chosen]
    else:
        print("要么给 --dir，要么用 --all；先看列表用 --list")
        return 1

    total_turns = 0
    passed_total = 0
    for direction, variant in jobs:
        print(f"\n{'=' * 70}\n▶ {direction} · {variant}"
              f"{'（放宽约束模式）' if args.relaxed else ''}\n{'=' * 70}")
        try:
            stats = framework.run_variant(direction, variant, limit=args.limit,
                                          relaxed=args.relaxed)
        except Exception as exc:
            print(f"！跑失败：{type(exc).__name__}: {exc}")
            continue
        total_turns += stats["turns"]
        passed_total += stats["passed"]
        print(f"　报告：{stats['report']}")

    print(f"\n{'=' * 70}")
    print(f"合计：{len(jobs)} 个变体、{total_turns} 轮，达标 {passed_total}/{total_turns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
