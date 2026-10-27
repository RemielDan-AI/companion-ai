# -*- coding: utf-8 -*-
"""端到端验证：问天气时，模型有没有把城市名说出来。

背景（2026-10-13 修的 bug）：猜不出城市时用默认城市查，但地点容易被模型省略，
于是「晴间多云，24.2℃」被用户当成自己所在地的天气。已从两处修：
  · `builtin._guess_city` 不再把时间词当城市；
  · `weather()` 输出固定以城市名开头；`weather.yaml` 的 prompt 明确要求带上城市名。

用法：
    set COMPANION_API_URL=http://127.0.0.1:8082/v1/chat/completions
    python tests/analysis/weather_e2e.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import Companion  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent.parent / "_tmp" / "weather"
QUESTIONS = [
    "今天天气怎么样",        # 没说城市 → 用默认城市，必须把城市名说出来
    "杭州天气怎么样",        # 说了城市
    "明天会下雨吗",          # 没说城市
]
CITY_RE = re.compile(r"(杭州|北京|上海|广州|深圳|成都|南京|武汉|西安|重庆|苏州|"
                     r"天津|长沙|青岛|厦门|昆明)")


def main() -> int:
    print(f"API：{os.environ.get('COMPANION_API_URL') or '（默认）'}")
    TMP.mkdir(parents=True, exist_ok=True)
    paths = {k: TMP / f"w_{k}" for k in
             ("db", "i.json", "p.json", "v.yaml", "s.jsonl", "pr.jsonl", "a.yaml")}
    for p in paths.values():
        if p.exists():
            p.unlink()
    companion = Companion(memory_path=paths["db"], index_path=paths["i.json"],
                          profile_path=paths["p.json"], values_path=paths["v.yaml"],
                          scenarios_path=paths["s.jsonl"],
                          preferences_path=paths["pr.jsonl"],
                          ai_profile_path=paths["a.yaml"])
    bad = 0
    try:
        for q in QUESTIONS:
            reply, _ = companion.say(q)
            skill = companion.last_decision.skill_id if companion.last_decision else "-"
            cities = CITY_RE.findall(reply or "")
            ok = bool(cities)
            if not ok:
                bad += 1
            print(f"\n{'✅' if ok else '✗'} 问：{q}　（技能 {skill}）")
            print(f"   答：{reply}")
            print(f"   提到的城市：{cities or '（没提，用户会误以为是本地天气）'}")
    finally:
        companion.memory.close()
    print(f"\n{'✅ 城市名都带上了' if bad == 0 else f'⚠ {bad} 轮没带城市名'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
