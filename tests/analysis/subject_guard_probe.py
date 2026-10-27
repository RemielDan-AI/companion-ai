"""看主体校验到底拦了什么——逐轮跑真实抽取，把**审计日志里的 skip** 打出来。

用途：主体校验（`_subject_ok`）是新加的最后一道闸门，
一旦它误杀，症状是"记忆莫名其妙变少"而**不留痕迹**。
这个探针把每一条被拦的原因摊开，用来区分：
    · 该拦的（第三方的事被记成用户的）
    · **误杀**（正常事实被挡掉 → 丢记忆，比漏拦更糟）

用法：
    python tests/model_server.py run --model v1.3 --ngl 15 \
        --script tests/analysis/subject_guard_probe.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TURNS = ["我妹妹在广州上学", "我妹妹叫林小雨", "小雨是广州大学的学生"]


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    try:
        from services.autostart import ensure_extract_service
        r = ensure_extract_service(verbose=False)
        print(f"抽取器：{r['action']}")
        os.environ["EXTRACT_URL"] = "http://127.0.0.1:8083/v1/chat/completions"
    except Exception as exc:
        print(f"抽取器没起来（{exc}）")

    import app as A
    d = pathlib.Path(tempfile.mkdtemp())
    c = A.Companion(memory_path=d / "m.db", index_path=d / "i.json",
                    profile_path=d / "p.json", values_path=d / "v.yaml",
                    scenarios_path=d / "sc.jsonl", preferences_path=d / "pf.jsonl",
                    ai_profile_path=d / "ai.json")

    for i, u in enumerate(TURNS, 1):
        info = c.flush_turn(u) if hasattr(c, "flush_turn") else None
        c.say(u)
        print(f"\n第{i}轮：{u}")

    print("=" * 92)
    print("语义库里现在有：")
    for it in c.memory.all_semantic(status="active"):
        print(f"  [{it['kind']:>9}] {it['content']}")

    print()
    print("=" * 92)
    print("审计日志（看 skip 拦了什么、为什么）")
    print("=" * 92)
    for row in c.memory.recent_logs(60):
        action = str(row.get("action") or "")
        if action in ("skip", "fix", "alias", "alias_reject", "merge_skip"):
            print(f"  [{action:>12}] {str(row.get('detail') or '')[:78]}")

    print()
    print("=" * 92)
    from cognition import alias as AL
    print(f"别名表：{[(r['alias'], r['canonical']) for r in AL.all_aliases(c.memory)]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
