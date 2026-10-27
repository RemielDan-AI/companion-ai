# -*- coding: utf-8 -*-
"""weather / _guess_city 回归（2026-10-13 修 bug 后补）。

修的 bug：用户没说城市时，把默认城市（杭州）的天气当成"用户所在地的天气"说出来。
根因有两条：
  ① `_guess_city` 会把时间词当城市——「今天天气怎么样」猜出城市 **"今天"**，
     地理编码查不到 → 抛错 → 技能降级成「天气我这儿没查到」；
  ② 猜不出城市时用默认城市，但输出「杭州现在…」的地点很容易被模型省略，
     于是「晴间多云，24.2℃」被用户当成自己所在地的天气。

本测试不联网：只验 `_guess_city` 的判定，以及 `weather` 输出里**必须带城市名**。
用 monkeypatch 掉网络调用。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import builtin  # noqa: E402

# ---- ① _guess_city：该认的认，不该认的必须返回空 ----
SHOULD_GUESS = [
    ("杭州天气怎么样", "杭州"),
    ("北京今天天气如何", "北京"),
    ("上海冷不冷", "上海"),
    ("帮我查一下成都的天气", "成都"),
    ("深圳明天下雨吗", "深圳"),
    ("西安气温多少度", "西安"),
]
SHOULD_NOT_GUESS = [
    "今天天气怎么样",          # ← 就是这条原来猜成了"今天"
    "今天天气还行",
    "明天会下雨吗",
    "这两天冷不冷",
    "外面天气如何",
    "现在气温多少度",
    "最近天气怎么样",
    "这里天气怎么样",
    "天气怎么样",
]


def main() -> int:
    bad = 0
    print("=== ① _guess_city 该认的 ===")
    for text, want in SHOULD_GUESS:
        got = builtin._guess_city(text)
        flag = "✅" if got == want else "✗"
        if got != want:
            bad += 1
        print(f"  {flag} {text!r} → {got!r}（期望 {want!r}）")

    print("\n=== ② _guess_city 不该认的（必须空串）===")
    for text in SHOULD_NOT_GUESS:
        got = builtin._guess_city(text)
        flag = "✅" if got == "" else "✗"
        if got != "":
            bad += 1
        print(f"  {flag} {text!r} → {got!r}")

    print("\n=== ③ weather 输出必须带城市名（离线桩）===")
    origin_geo, origin_fc = builtin._get_json, builtin._get_json

    def fake_get(url: str, timeout: float = 10) -> dict:
        if "geocoding" in url:
            return {"results": [{"name": "杭州", "latitude": 30.2, "longitude": 120.1}]}
        return {"current": {"temperature_2m": 24.2, "apparent_temperature": 26.2,
                            "weather_code": 1, "wind_speed_10m": 5.7}}

    builtin._get_json = fake_get
    try:
        out = builtin.weather("今天天气怎么样")      # 猜不出城市 → 用默认
        print(f"  未指定城市 → {out!r}")
        if "杭州" not in out:
            print("  ✗ 输出里没有城市名（用户会误当成自己所在地）")
            bad += 1
        else:
            print("  ✅ 带上了城市名")
        out2 = builtin.weather("北京天气怎么样")
        print(f"  指定城市   → {out2!r}")
    finally:
        builtin._get_json = origin_geo

    print("\n" + ("✅ 全部通过" if bad == 0 else f"⚠ {bad} 项不合格"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
