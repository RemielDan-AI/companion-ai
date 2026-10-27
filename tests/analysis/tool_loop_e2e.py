#!/usr/bin/env python
"""端到端：走真实 Companion，温度固定 0，看模型自选工具能否真正执行。

与 `tool_loop_live.py` 的区别：那个脚本是**直接调模型**验证"会不会吐块"；
这个走**真实主链路**（技能路由 → 决策 → 提示组装 → 循环 → 真工具执行 → 收尾），
验证"接了线之后到底动不动"，而且把温度钉成 0 去掉随机性。

看三个指标：
  · 有几个问法真的执行了工具（`last_tool_loop.calls`）
  · 最终回复是不是正文（不含协议标记）
  · 技能日志有没有落 `model_choice`

用法：python tests/analysis/tool_loop_e2e.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.protocol import END, START  # noqa: E402

QUERIES = ["外面冷吗", "要穿外套吗", "现在几度", "今天外面要穿外套吗"]
TMP = HERE.parent / "_tmp" / "tool_e2e"


def main() -> int:
    import app as app_module
    from cognition import linkage

    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    comp = app_module.Companion(
        memory_path=TMP / "m.db", index_path=TMP / "i.json",
        profile_path=TMP / "p.json", values_path=TMP / "v.yaml",
        scenarios_path=TMP / "sc.jsonl", preferences_path=TMP / "pf.jsonl",
        ai_profile_path=TMP / "ai.json")
    # 温度钉成 0：去掉采样随机性，让"两次跑出来的差异"只可能来自真实原因
    _T0 = {level: {"temperature": 0.0, "repeat_penalty": 1.15,
                   "max_tokens": tokens}
           for level, tokens in (("l0", 128), ("l1", 384),
                                 ("l2", 640), ("l3", 900))}

    def level_info_zero(level):
        return _T0.get(level, _T0["l1"])

    comp.generator.level_info = level_info_zero

    print(f"温度固定 0，问法：{QUERIES}")
    print()
    executed = 0
    for text in QUERIES:
        try:
            reply, _ = comp.say(text)
        except Exception as exc:
            print(f"「{text}」 ✗ 报错：{type(exc).__name__}: {exc}")
            continue
        loop = getattr(comp, "last_tool_loop", None)
        calls = list(loop.calls) if loop else []
        if calls:
            executed += 1
        leaked = START in (reply or "")
        print(f"「{text}」")
        print(f"    路由={comp.last_decision.skill_id}/{comp.last_decision.kind}"
              f"　工具调用={len(calls)} 次"
              + (f"　{[c['name'] for c in calls]}" if calls else ""))
        if calls:
            for c in calls:
                print(f"      {c['name']}{c['args']} ok={c['ok']} "
                      f"→ {str(c['text'])[:60]}")
        print(f"    回复：{(reply or '（空）')[:90]}")
        if leaked:
            print("    ⚠ 回复里泄漏了协议标记！")

    print()
    print("=" * 78)
    print(f"真的有工具执行：{executed}/{len(QUERIES)}")
    logs = linkage.skill_logs(comp.memory, limit=10)
    mine = [x for x in logs if str(x.get("skill_id")) == "model_choice"]
    print(f"技能日志里的 model_choice：{len(mine)} 条")
    for x in mine:
        print(f"    tool={x.get('tool')} detail={x.get('detail')} ok={x.get('ok')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
