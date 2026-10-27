#!/usr/bin/env python
"""诊断「地点被省略」：把易丢地点的问法重复采样，看失败样本长什么样。

## 背景

`tool_city_probe.py` 3 次重复的结果：整体地点保留 14/18，但**问法之间差别很大**：
    外面冷吗 3/3　北京今天冷吗 3/3　现在几度 2/3　今天外面要穿外套吗 2/3
    **要穿外套吗 1/3**  ← 最差
两种假说要分开验证：
    ① **训练样本太薄**：S2 每个问法只有 1 条样本，行为没学稳 → 概率性丢。
       （「外面冷吗」在 S2 里有 2 条 → 它就是 3/3）
    ② **风格上限逼短**：风格块压字数，模型为了短把城市砍掉 → 失败样本应**更短**。

本脚本把每个易丢问法重复 N 次，记录：是否带地点、字数、有没有说「我这边/我这儿」
（「我这边」= 把工具返回的城市换成自己所在地，正是老坑的形态）。

用法：
    set PROBE_RELAXED=0
    set PROBE_REPEAT=6
    python tests/model_server.py run --model v1.3 --ngl 99 \
        --script tests/analysis/diagnose_city_drop.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CITIES = ("杭州", "北京", "上海", "成都", "广州", "南京", "深圳", "武汉")
SELF_MARKS = ("我这边", "我这儿", "这边是", "我这")

# 易丢地点的问法（按 3 次采样的表现排序，最差在前）
CASES = ["要穿外套吗", "现在几度", "今天外面要穿外套吗", "外面冷吗"]
TMP = HERE.parent / "_tmp" / "citydrop"
_serial = [0]


def make():
    import app as app_module
    _serial[0] += 1
    d = TMP / f"c{_serial[0]}"
    d.mkdir(parents=True, exist_ok=True)
    comp = app_module.Companion(
        memory_path=d / "m.db", index_path=d / "i.json",
        profile_path=d / "p.json", values_path=d / "v.yaml",
        scenarios_path=d / "sc.jsonl", preferences_path=d / "pf.jsonl",
        ai_profile_path=d / "ai.json")
    comp.rules_relaxed = os.environ.get("PROBE_RELAXED", "1") != "0"
    return comp


def main() -> int:
    repeat = max(1, int(os.environ.get("PROBE_REPEAT", "6")))
    print(f"每问重复 {repeat} 次；只看地点有没有被省略")
    print("=" * 84)
    rows = []
    for text in CASES:
        for i in range(1, repeat + 1):
            comp = make()
            try:
                reply, _ = comp.say(text)
            except Exception as exc:
                print(f"「{text}」#{i} ✗ {type(exc).__name__}: {exc}")
                continue
            reply = (reply or "").strip()
            loop = getattr(comp, "last_tool_loop", None)
            calls = list(loop.calls) if loop else []
            city = ""
            if calls:
                m = re.search(r"([\u4e00-\u9fa5]{2,4})今天|([\u4e00-\u9fa5]{2,4})明天", 
                              str(calls[-1]["text"]))
                city = (m.group(1) or m.group(2)) if m else ""
            kept = bool(city) and city in reply
            selfmark = next((s for s in SELF_MARKS if s in reply), "")
            rows.append({"q": text, "i": i, "called": bool(calls), "city": city,
                         "kept": kept, "len": len(reply), "self": selfmark,
                         "reply": reply})
            flag = "✅ 带地点" if kept else ("✗ **丢了**" if calls else "· 没调工具")
            print(f"「{text}」#{i} {flag}　{len(reply)} 字"
                  + (f"　用了「{selfmark}」" if selfmark else ""))
            print(f"      {reply[:88]}")
        print("-" * 84)

    print("=" * 84)
    ok = [r for r in rows if r["kept"]]
    bad = [r for r in rows if r["called"] and not r["kept"]]
    called = [r for r in rows if r["called"]]
    print(f"调了工具 {len(called)}/{len(rows)}　带地点 {len(ok)}/{len(called)}"
          f"　丢了 {len(bad)}")
    if ok and bad:
        print(f"字数：带地点 平均 {sum(r['len'] for r in ok) / len(ok):.1f} 字"
              f"　丢地点 平均 {sum(r['len'] for r in bad) / len(bad):.1f} 字")
        print("  → 若「丢地点」明显更短，说明是**风格压短**导致的；"
              "若差不多，说明是**样本太薄、概率性丢**。")
    if bad:
        print()
        print("丢地点的样本里用了「我这边」这类说法的："
              f"{sum(1 for r in bad if r['self'])}/{len(bad)}")
        print("  → 用「我这边」= 把工具返回的城市换成了自己所在地（老坑的形态）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
