"""对照测试的冒烟检查：同一个模型问同样几句话，确认能装载、不空答、不串题。

用法：
    set COMPANION_API_URL=http://127.0.0.1:8082/v1/chat/completions
    python tests/compare_smoke.py

看的是"这条路通不通"：有没有空回复、有没有把上一题的答案端上来（历史上 1.5B
去对齐版就栽在串题）、有没有夹杂英文、耗时量级。
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import Companion  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "smoke"

PROMPTS = [
    ("情绪", "今天上班被领导当着一屋子人骂了，回来什么也不想干。"),
    ("自贬", "你说我是不是特别没出息。"),
    ("闲聊", "晚上吃点什么好？"),
]


def main() -> int:
    api = os.environ.get("COMPANION_API_URL")
    print(f"API：{api or '（未设置，用 config/model.yaml 里的地址）'}")
    TMP.mkdir(parents=True, exist_ok=True)
    paths = {k: TMP / f"smoke_{k}" for k in
             ("db", "index.json", "profile.json", "values.yaml",
              "scenarios.jsonl", "preferences.jsonl")}
    for path in paths.values():
        if path.exists():
            path.unlink()

    companion = Companion(memory_path=paths["db"], index_path=paths["index.json"],
                          profile_path=paths["profile.json"],
                          values_path=paths["values.yaml"],
                          scenarios_path=paths["scenarios.jsonl"],
                          preferences_path=paths["preferences.jsonl"])
    ok = 0
    try:
        for tag, prompt in PROMPTS:
            started = time.time()
            reply, info = companion.say(prompt)
            cost = time.time() - started
            empty = not (reply or "").strip()
            latin = sum(1 for ch in reply if ch.isascii() and ch.isalpha())
            print(f"\n[{tag}] 用户：{prompt}")
            print(f"      未央：{reply!r}")
            print(f"      {len(reply)} 字 · {cost:.1f}s · 档位 "
                  f"{companion.last_level or '-'} · 技能 "
                  f"{(companion.last_decision.skill_id if companion.last_decision else '-')}"
                  + f" · 英文占比 {latin / max(1, len(reply)) * 100:.1f}%"
                  + ("　⚠ 空回复" if empty else ""))
            ok += 0 if empty else 1
    finally:
        companion.memory.close()

    print(f"\n非空回复 {ok}/{len(PROMPTS)}")
    return 0 if ok == len(PROMPTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
