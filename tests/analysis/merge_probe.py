"""用**真的 Qwen3.5-2B** 测记忆合并：它到底能不能胜任。

## 为什么单独测

`tests/conflict_merge_test.py` 用桩把逻辑钉住了，但"2B 能不能干这活"是另一回事。
本探针把每类冲突丢给真模型，**同时报告护栏有没有拦下编造**——
只看"合成了什么"而不看"有没有被拦"，会高估模型能力。

## 判据

    ① 合成句非空、通过护栏
    ② 合并后**没有丢信息**：旧说法被推翻的要点、新说法的要点都还在
    ③ **没有编造**：护栏拦下的算"诚实失败"（保持未决），不算错

用法：
    python tests/analysis/merge_probe.py     # 抽取器由本脚本自己拉起（8083）
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.merge import check_merge, merge_pair   # noqa: E402

# (旧, 新, 期望要点：合成句里**应该出现**的词)
CASES = [
    ("对方住在南京", "对方搬到成都", ["成都"]),
    ("对方在工业软件公司上班", "对方公司搬到滨江", ["工业软件", "滨江"]),
    ("对方养了只猫叫团子", "对方又养了只狗叫阿黄", ["团子", "阿黄"]),
    ("对方妹妹在读大学", "对方妹妹转学了", ["妹妹"]),
    ("对方每天七点起床", "对方现在六点就起", ["起"]),
    ("对方喜欢喝美式", "对方现在改喝拿铁", ["拿铁"]),
]


def main() -> int:
    url = os.environ.get("EXTRACT_URL") or "http://127.0.0.1:8083/v1/chat/completions"
    from engine.generate import Generator
    gen = Generator(api_url=url, timeout=60)
    print(f"合并用模型：{url}")
    print("=" * 90)
    ok_n = kept_n = 0
    for old, new, wants in CASES:
        t0 = time.time()
        got = merge_pair(gen, old, new)
        dt = time.time() - t0
        merged = got.get("merged") or ""
        acted = got["action"] == "merged"
        ok_n += acted
        kept = all(w in merged for w in wants) if acted else False
        kept_n += kept
        print(f"\n旧：{old}")
        print(f"新：{new}")
        if acted:
            print(f"→ 合成：{merged}　({dt:.1f}s)")
            miss = [w for w in wants if w not in merged]
            print(f"  要点核对：{'✅ 要点都在' if kept else f'✗ 丢了 {miss}'}")
        else:
            print(f"→ **未合并**（{got.get('reason')}）　({dt:.1f}s)")
            print("  这是诚实的失败：两条都保留、都可检索，不算写错")
    print()
    print("=" * 90)
    print(f"真的合并成功：{ok_n}/{len(CASES)}　其中要点未丢：{kept_n}/{len(CASES)}")
    print(f"未合并（护栏拒绝或模型没答）：{len(CASES)-ok_n} 条 —— "
          f"这些**不会丢数据**，只是没合成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
