"""确定性自检：同一句话反复路由，结果必须完全一致（不调模型，秒级）。

边界最怕的不是"稳定漏掉某一类"，而是"同一句话今天拒、明天答"。
这个脚本把覆盖体检里的 46 条句子各路由 20 次，任何一次结果不同就报错。

用法：python tests/determinism_check.py [次数]
"""
from __future__ import annotations

import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import Companion  # noqa: E402
from tests.cover_check import CASES  # noqa: E402


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    tmp = pathlib.Path(__file__).resolve().parent / "_tmp" / "determ"
    tmp.mkdir(parents=True, exist_ok=True)
    companion = Companion(memory_path=tmp / "db", index_path=tmp / "i.json",
                          profile_path=tmp / "p.json", values_path=tmp / "v.yaml",
                          scenarios_path=tmp / "s.jsonl",
                          preferences_path=tmp / "pr.jsonl",
                          ai_profile_path=tmp / "a.yaml")

    unstable = []
    for category, text in CASES:
        results = Counter()
        for _ in range(rounds):
            decision = companion.router.route(text)
            results[(decision.kind, str(decision.skill_id))] += 1
        if len(results) > 1:
            unstable.append((category, text, dict(results)))
            print(f"✗ 不稳定：{text[:34]}")
            for key, count in results.items():
                print(f"      {key} × {count}")

    companion.memory.close()
    print(f"\n共 {len(CASES)} 条句子 × {rounds} 次路由")
    if unstable:
        print(f"不稳定 {len(unstable)} 条 —— 边界没有确定性保证")
        return 1
    print("全部一致 ✓ 路由是确定的（同一句话永远得到同一个决定）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
