#!/usr/bin/env python
"""隔离验证：**不谈天气的话，模型会不会自己扯出温度和城市**（疑似编造的副作用）。

## 起因

`tool_city_probe.py` 里「我今天有点累」的回复是：
    「那就歇会儿吧。外面 23 度多，穿件外套就行。你说的我都记住了：**杭州冷一点**，要照顾好自己。」
但那一问是**全新 Companion、空记忆**，用户从没提天气或杭州——
「23 度」「杭州」都无从谈起。怀疑是**工具调用训练过拟合了天气模式**，
把「温度 + 城市」这一套带到了无关话题里。

## 方法

**一问一进程内的第一个问法**（前面不跑任何天气问），空记忆，看回复里有没有
温度数字/城市名。多个问法各起一个 Companion。
同时把「空库 + 无工具说明」这条钉死，排除"上一问残留"的可能。

用法：python tests/analysis/no_weather_bleed_probe.py
"""
from __future__ import annotations

import pathlib
import re
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.tool_loop import needs_model_tools          # noqa: E402

# 都不该出现天气/城市
CASES = [
    "我今天有点累",
    "我最近压力好大",
    "在吗",
    "陪我聊会儿天吧",
    "我今天很开心",
    "我妹妹叫什么",
]
CITIES = ("杭州", "北京", "上海", "成都", "广州", "南京", "深圳", "武汉")
TEMP_RE = re.compile(r"\d+\s*(度|℃|摄氏度)|气温|温度|天气|穿件|加件|带伞")
TMP = HERE.parent / "_tmp" / "noweather"


_serial = [0]


def make():
    """每个问法一个**独立目录**。

    ⚠ 不能 rmtree 同一个路径再新建：Windows 上 sqlite 文件被前一个 Companion
    的 conn 占着，rmtree 会**静默失败**（ignore_errors=True），
    于是"新建的 Companion"其实复用了上一个库、连同它的记忆——
    我第一版就是这么写的，结果把上一问查到的天气当成了"编造"。
    """
    import app as app_module
    _serial[0] += 1
    d = TMP / f"c{_serial[0]}"
    d.mkdir(parents=True, exist_ok=True)
    return app_module.Companion(
        memory_path=d / "m.db", index_path=d / "i.json",
        profile_path=d / "p.json", values_path=d / "v.yaml",
        scenarios_path=d / "sc.jsonl", preferences_path=d / "pf.jsonl",
        ai_profile_path=d / "ai.json")


def main() -> int:
    print("每个问法各起一个**全新空库** Companion；这些问法都不该提到天气/城市")
    print("=" * 78)
    bleed = []
    for text in CASES:
        comp = make()
        try:
            reply, _ = comp.say(text)
        except Exception as exc:
            print(f"「{text}」✗ {type(exc).__name__}: {exc}")
            continue
        reply = reply or ""
        found_city = [c for c in CITIES if c in reply]
        found_wx = TEMP_RE.findall(reply)
        gate = needs_model_tools(text, router_fired=False)
        bad = bool(found_city) or bool(found_wx)
        if bad:
            bleed.append(text)
        print(f"「{text}」　门槛挂工具={gate}　"
              + ("⚠ **出现了天气/城市**" if bad else "✅ 干净"))
        print(f"    回复：{reply[:96]}")
        if found_city or found_wx:
            print(f"    命中：城市={found_city} 天气词={found_wx}")
    print()
    print("=" * 78)
    print(f"出现天气/城市泄漏的问法：{len(bleed)}/{len(CASES)}")
    if bleed:
        print(f"  {bleed}")
        print("→ 说明工具调用的训练**过拟合了天气模式**，把「温度+城市」带进了无关话题。")
    else:
        print("  → 没有泄漏 ✅（先前看到的可能是测试串联导致的假象）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
