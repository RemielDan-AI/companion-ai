#!/usr/bin/env python
"""阶段 21 冷启动策略测试（项目改进v1 改进 26）。

验收：新用户不预设个人记忆、前几轮主动询问关键信息、逐步积累、第一周不推断价值观。

用法：python tests/stage21_onboarding_test.py
"""
from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition import onboarding  # noqa: E402
from cognition.memory import Memory  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "onboard"


def days_ago(n: float) -> str:
    return (dt.datetime.now() - dt.timedelta(days=n)).isoformat(timespec="seconds")


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 1. 全新用户：不预设记忆 ----
    fresh = Memory(TMP / "fresh.db")
    info = onboarding.stats(fresh)
    check("全新库没有任何个人记忆",
          fresh.episode_count() == 0 and fresh.semantic_count() == 0
          and not fresh.all_semantic(), "")
    check("阶段判定为全新", onboarding.stage(fresh) == "全新", onboarding.stage(fresh))
    check("第一周不推断价值观（全新时直接不允许）",
          onboarding.allow_values(fresh) is False, "")
    hint = onboarding.prompt_hint(fresh)
    check("第一次对话会提示主动问关键信息",
          "名字" in hint and "档案" in hint, hint[:50])
    check("提示里不要连珠炮", "连珠炮" in hint, "")
    check("一次只问一件",
          onboarding.next_question(fresh) == "名字", onboarding.next_question(fresh))

    # ---- 2. 初期：问过名字之后换下一件 ----
    fresh.add_episode("我叫林一", "记住了", summary="报名字", session="s", turn=1)
    fresh.upsert_semantic("fact", "对方叫林一", confidence=0.8)
    check("知道名字后不再问名字",
          onboarding.next_question(fresh) != "名字", onboarding.next_question(fresh))
    check("初期阶段判定正确", onboarding.stage(fresh) == "初期", onboarding.stage(fresh))
    check("初期仍然不推断价值观", onboarding.allow_values(fresh) is False, "")
    check("初期提示改为顺着聊不追问",
          "刚认识不久" in onboarding.prompt_hint(fresh), onboarding.prompt_hint(fresh)[:40])

    # ---- 3. 成长期：认识几天但仍不满一周 ----
    fresh.conn.execute("UPDATE episodic SET created_at=?", (days_ago(3),))
    fresh.conn.commit()
    check("三天时算成长期", onboarding.stage(fresh) == "成长期", onboarding.stage(fresh))
    check("三天时仍不推断价值观", onboarding.allow_values(fresh) is False, "")
    check("成长期提示不再追问",
          "不必刻意追问" in onboarding.prompt_hint(fresh), onboarding.prompt_hint(fresh)[:40])

    # ---- 4. 稳定期：过了第一周，价值观可以开始养成 ----
    fresh.conn.execute("UPDATE episodic SET created_at=?", (days_ago(8),))
    fresh.conn.commit()
    check("八天后算稳定期", onboarding.stage(fresh) == "稳定", onboarding.stage(fresh))
    check("过了第一周才允许推断价值观", onboarding.allow_values(fresh) is True, "")
    check("稳定期不再给冷启动提示", onboarding.prompt_hint(fresh) == "",
          onboarding.prompt_hint(fresh)[:30])

    # ---- 5. 关键信息识别与报告 ----
    fresh.upsert_semantic("preference", "对方喜欢喝美式咖啡", confidence=0.6)
    fresh.upsert_semantic("fact", "对方住在杭州", confidence=0.6)
    info2 = onboarding.stats(fresh)
    check("能识别出已知道的关键信息",
          "名字" in info2["known"] and "住处" in info2["known"]
          and "在意的事" in info2["known"], str(info2["known"]))
    check("还没问到的留在待问清单",
          "作息" in info2["missing"] and "工作" in info2["missing"], str(info2["missing"]))
    report = onboarding.report(fresh)
    check("冷启动报告能打印成人话",
          "阶段" in report and "已经知道" in report and "价值观" in report, report[:40])
    check("报告里说明第一周只记录",
          "第一周" in report or "已过第一周" in report, report[-60:])

    # ---- 6. 逐步积累：早期不写画像由 allow_values 与提示共同保证 ----
    early = Memory(TMP / "early.db")
    for turn in range(1, 4):
        early.add_episode(f"闲聊第 {turn} 句", "嗯", summary=f"闲聊{turn}", session="s",
                          turn=turn)
    check("初期对话照常记录（只是不推断价值观）",
          early.episode_count() == 3 and onboarding.allow_values(early) is False, "")
    check("初期阶段不会因为轮数多就跳过等待期",
          onboarding.stage(early) == "初期", f"{onboarding.stage(early)}／"
          f"{onboarding.stats(early)['turns']} 轮")

    fresh.close()
    early.close()
    shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"\n通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
