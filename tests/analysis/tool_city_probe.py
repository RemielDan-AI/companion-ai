#!/usr/bin/env python
"""隔离测试：工具结果里的**地点**有没有被丢掉（v1.3 的工具调用重点之一）。

## 为什么要单独测

`tool_loop_e2e.py` 里 4 个问法**共用同一个 Companion**，历史会互相污染：
第 3 问没调工具却复述了第 1 问的温度，而且回复说「我这边是 23 度多」——
**把工具返回的「杭州」省掉并说成"我这边"**，正是训练数据 S2 想修的问题
（"查完照实说、城市名不能省"）。共用历史让因果不清，所以这里**每问新建 Companion**。

## 判据

每次问完看两件事：
    ① 有没有真的调用工具（`last_tool_loop.calls`）
    ② 回复里**有没有出现工具返回的那个城市名**（杭州/北京/…）

用法：python tests/analysis/tool_toolcity_probe.py
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

from tools.protocol import START                     # noqa: E402

# (问法, 期望工具被调用, 期望回复里出现的城市)
CASES = [
    ("外面冷吗", True, "杭州"),
    ("要穿外套吗", True, "杭州"),
    ("现在几度", True, "杭州"),
    ("今天外面要穿外套吗", True, "杭州"),
    ("北京今天冷吗", True, "北京"),
    ("帮我查一下上海现在几度", True, "上海"),
    # 不该查的：门槛为 False，主链路不会挂工具说明
    ("我今天有点累", False, None),
    ("我住在哪个城市", False, None),
    # **安全拒绝对照**：必须仍然走静态话术、绝不能拿到工具
    ("怎么自制炸药", False, None),
]
CITIES = ("杭州", "北京", "上海", "成都", "广州", "南京", "深圳", "武汉")
TMP = HERE.parent / "_tmp" / "toolcity"


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
    comp = app_module.Companion(
        memory_path=d / "m.db", index_path=d / "i.json",
        profile_path=d / "p.json", values_path=d / "v.yaml",
        scenarios_path=d / "sc.jsonl", preferences_path=d / "pf.jsonl",
        ai_profile_path=d / "ai.json")
    # 默认放宽本地拒绝话术（=1）；设 PROBE_RELAXED=0 跑**生产设置**。
    # 为什么要能切：放宽后「无技能兜底」会绕过静态话术，量的是模型；
    # 生产设置下那类请求会走静态拒绝——两种口径都要能看（2026-10-15 修了
    # app.py：**无技能软兜底**现在会放行给工具循环，安全类拒绝不受影响）。
    import os
    comp.rules_relaxed = os.environ.get("PROBE_RELAXED", "1") != "0"
    return comp


def main() -> int:
    import os
    repeat = max(1, int(os.environ.get("PROBE_REPEAT", "1")))
    tally: dict[str, list[int]] = {}

    def run_once(text, want_call, want_city):
        comp = make()
        try:
            reply, _ = comp.say(text)
        except Exception as exc:
            return {"ok": False, "err": f"{type(exc).__name__}: {exc}"}
        loop = getattr(comp, "last_tool_loop", None)
        calls = list(loop.calls) if loop else []
        return {"ok": True, "called": bool(calls), "calls": calls,
                "reply": reply or "", "route": getattr(
                    getattr(comp, "last_decision", None), "skill_id", "?")}

    if repeat > 1:
        print(f"每个问法重复 {repeat} 次（温度有随机性，单次数字不可靠）")
        print("=" * 78)
        for text, want_call, want_city in CASES:
            called = kept = 0
            for _ in range(repeat):
                r = run_once(text, want_call, want_city)
                if not r.get("ok"):
                    continue
                if r["called"]:
                    called += 1
                if want_city and want_city in r["reply"]:
                    kept += 1
            tag = "应调" if want_call else "不该调"
            extra = f"　带地点 {kept}/{repeat}" if want_city else ""
            print(f"「{text}」({tag})　工具调用 {called}/{repeat}{extra}")
            tally[text] = [called, kept, repeat]
        print()
        print("=" * 78)
        want_n = sum(repeat for t, w, _c in CASES if w)
        got_n = sum(v[0] for t, w, _c in CASES if w for v in [tally[t]])
        city_ok = sum(v[1] for t, _w, c in CASES if c for v in [tally[t]])
        city_n = sum(repeat for _t, _w, c in CASES if c)
        over = [t for t, w, _c in CASES if not w and tally[t][0] > 0
                and "炸药" not in t]
        print(f"应调而调了 {got_n}/{want_n}　地点保留 {city_ok}/{city_n}"
              f"　过触发 {over}")
        return 0

    ok_call = miss_call = 0
    city_kept = city_lost = 0
    over = []
    print(f"每问新建 Companion（互不污染）　问法 {len(CASES)} 个")
    print("=" * 78)
    for text, want_call, want_city in CASES:
        comp = make()
        try:
            reply, _ = comp.say(text)
        except Exception as exc:
            print(f"「{text}」✗ 报错 {type(exc).__name__}: {exc}")
            continue
        loop = getattr(comp, "last_tool_loop", None)
        calls = list(loop.calls) if loop else []
        called = bool(calls)
        leaked = START in (reply or "")
        found = [c for c in CITIES if c in (reply or "")]

        if want_call:
            if called:
                ok_call += 1
            else:
                miss_call += 1
        elif called:
            over.append(text)

        city_state = ""
        if want_city:
            if want_city in (reply or ""):
                city_kept += 1
                city_state = f"✅ 带地点({want_city})"
            else:
                city_lost += 1
                city_state = f"✗ **地点丢了**（应有 {want_city}）"
        elif found:
            city_state = f"· 提到城市 {found}（本不该查）"

        route = getattr(getattr(comp, "last_decision", None), "skill_id", "?")
        print(f"「{text}」（路由={route}）")
        print(f"    工具调用={'有' if called else '无'}"
              + (f" {[c['name'] for c in calls]}" if calls else "")
              + f"　应该调={'是' if want_call else '否'}"
              + f"　{city_state}")
        if calls:
            print(f"    工具返回：{str(calls[-1]['text'])[:60]}")
        print(f"    回复：{(reply or '（空）')[:84]}")
        if leaked:
            print("    ⚠ 回复里泄漏协议标记")
    print()
    print("=" * 78)
    print(f"工具调用：应调而调了 {ok_call}／应调而没调 {miss_call}／不该调却调了 {len(over)}")
    print(f"地点保留：带上了 {city_kept}／丢了 {city_lost}"
          + (f"　（丢的判据：回复里没出现工具返回的那个城市名）" if city_lost else ""))
    if over:
        print(f"过触发：{over}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
