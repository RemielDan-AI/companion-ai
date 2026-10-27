"""钉住「时间技能」：工具函数 + 路由触发（不连模型）。

## 背景

系统提示里本来就有 `engine/generate.py: now_line()`，模型多数时候读得出来
（「现在几点」→「20:16 周三了」✅）。但**问"几号"会答错**：
实测「今天几号」→「周三了。」——把"号"当成了周几。

所以加了 `skills/core/clock.yaml` + `tools/builtin.py: time_now()`：
把年/月/日/时/分/星期/第几周/时段都摆清楚，**答案由程序算**。

## 这个测试盯两件事

    ① **工具函数**：返回里必须真的有当前的年月日、星期、时分
    ② **路由触发**：
       · 时间类必须命中 S005
       · **「今天好累」「今天下雨了」这类绝不能被时间吸走**（最怕的过触发）
       · 天气 / 提醒 / 越狱 不能被时间抢

⚠ 第 ② 条里"不许命中"的几条是重点：`今天` 是极高频词，
   当初就差点把 `今天` 直接写成关键词——那样「今天好累」会被当成问日期。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from router.skill_router import SkillRouter   # noqa: E402
from tools.builtin import time_now            # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 88)
    print("时间技能：工具函数 + 路由触发")
    print("=" * 88)

    now = dt.datetime.now()
    wk = "一二三四五六日"[now.weekday()]
    out = time_now()

    # ---- ① 工具函数 ----
    check(str(now.year) in out, "工具含年份", str(now.year))
    check(f"{now.month} 月" in out or f"{now.month}月" in out, "工具含月份", str(now.month))
    check(f"{now.day} 日" in out or f"{now.day}日" in out, "工具含日期", str(now.day))
    check(f"{now.hour:02d}:{now.minute:02d}" in out, "工具含时分",
          f"{now.hour:02d}:{now.minute:02d}")
    check(f"星期{wk}" in out, "工具含星期", wk)
    check("第" in out and "周" in out, "工具含今年第几周")

    # ---- ② 路由 ----
    r = SkillRouter()

    def sid(text: str) -> str:
        return str(getattr(r.route(text), "skill_id", "?"))

    hit_clock = ["现在几点", "今天几号", "今天周几", "现在是什么时候了",
                 "今年是哪一年", "明天几号", "几点了", "第几周"]
    for q in hit_clock:
        check(sid(q) == "S005", f"时间类命中：{q}", sid(q))

    # ⚠ 最要紧的：`今天` 太常见，绝不能因为一个"今天"就当成问时间
    must_not = {
        "今天好累": "闲聊/情绪",
        "今天下雨了": "天气",
        "我昨天去了趟超市": "闲聊",
        "今天开会开到现在": "闲聊/情绪",
    }
    for q, why in must_not.items():
        check(sid(q) != "S005", f"**不许**命中时间：{q}", f"{sid(q)}（{why}）")

    # 别的技能不能被时间抢
    check(sid("提醒我明天买菜") == "S004", "提醒仍是提醒", sid("提醒我明天买菜"))
    check(sid("外面天气怎么样") == "E001", "天气仍是天气", sid("外面天气怎么样"))

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
