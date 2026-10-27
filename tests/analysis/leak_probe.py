"""定位「漏进库」的那两条：谁把内容挡掉的。

抽取质量指标（`tests/analysis/extract_metric.py`）第二阶段发现两条**真的漏进库**：

    ① 「我妹妹不吃香菜」→ 入库只剩 `对方提到的人：妹妹（用户称呼）`，**香菜丢了**
    ② 「我住在南京」    → **整句没进库**

本探针把这两句的**原始抽取** + **每道闸门的判定** + **审计日志**全打出来，
定位到底是模型没抽出来，还是被规则误杀。

用法：
    python tests/model_server.py run --model Qwen3.5-2B --ngl 99 \
        --script tests/analysis/leak_probe.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from cognition.extract import remember_turn            # noqa: E402
from cognition.memory import Memory                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

CASES = [
    ("我妹妹不吃香菜", "香菜我记着。"),
    ("我住在南京", "南京，记下了。"),
]

REPEAT = max(1, int(os.environ.get("LEAK_REPEAT", "2")))


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"漏库定位　模型：{os.environ.get('EXTRACT_MODEL_TAG') or url}")
    print("=" * 96)
    for user, reply in CASES:
        print(f"\n{'=' * 96}\n用户：{user}")
        for i in range(1, REPEAT + 1):
            raw = EX.extract_turn(gen, user, reply)
            print(f"\n  ── 第 {i} 次 ──")
            print(f"  原始 facts     = {[str(x) for x in (raw.get('facts') or [])]}")
            print(f"  原始 relations = {[str(x) for x in (raw.get('relations') or [])]}")
            print(f"  原始 prefs     = {[str(x) for x in (raw.get('preferences') or [])]}")

            if raw.get("relations"):
                kept, rec = EX._reclassify_state_relations(raw["relations"], user)
                print(f"  状态句还原：保留={kept} 还原={rec}")
            if raw.get("facts"):
                out, filled = EX._complete_state_facts(raw["facts"], user)
                print(f"  缺宾语补全：{raw['facts']} → {out}（补了 {filled}）")

            d = pathlib.Path(tempfile.mkdtemp())
            mem = Memory(str(d / "m.db"))
            remember_turn(mem, gen, user, reply, session="s", turn=1, index=None)
            stored = [f"[{x['kind']}] {x['content']}"
                      for x in mem.all_semantic(status="active")]
            print(f"  **入库** = {stored}")
            skips = [r for r in mem.recent_logs(30)
                     if str(r.get("action")) in ("skip", "fix")]
            for r in skips:
                print(f"     [{r['action']}] {str(r.get('detail'))[:74]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
