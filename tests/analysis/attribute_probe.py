"""量「编属性」：回复里凭空出现证据中没有的数字（年龄/次数/金额/人数）。

## 为什么要单独一个探针

`fabrication_probe.py` 量的是**编前情**（「你上次也…」），那是**过去指涉**。
这次是**编属性**——实测撞到：

    问「今年是哪一年」
    → 「2026 年——**你还没满 18 周岁呢**」

证据里只有年份和时分，「18」哪儿都没有。这类编造**随着功能变多会越来越多**
（每个新技能/新数据源都是一个可以编的面），用户一旦被安上一个没说过的事实，
信任掉得最快。

## 两组对照

    **A 组（无凭据）**：空记忆 + 中性话题，问**不需要数字**的事
        → 回复里出现证据没有的数字 = 编属性，目标 0

    **B 组（有凭据）**：给含数字的记忆/时间，问相应的事
        → 回复**应该**带那个数字，不能被压掉
        ⚠ 没有 B 组就会犯"禁词"的老错：把数字全禁了，连"三岁""周三"都不能说。

## 判据

`cognition/verify.py: unsupported_numbers(evidence, reply)` ——
回复里断言了、证据里找不到的数字。**只查数字**，因为中文专名没有可靠的确定性做法，
而数字是最高精度的那一类（编造的属性几乎都带数字）。
"""
from __future__ import annotations

import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import Companion                                   # noqa: E402
from cognition.verify import unsupported_numbers            # noqa: E402

RUNS = max(1, int(os.environ.get("ATTR_RUNS", "2")))

# A 组：这些话题**不需要任何数字**，回复里冒数字就是编的
NEUTRAL = [
    "今天累死了",
    "刚跟朋友吵了一架",
    "我在想要不要换工作",
    "我最近老是想哭",
    "周末不知道干嘛好",
    "我妈又催我相亲了",
    "你觉得我该不该换城市",
    "我有点睡不着",
]

# B 组：有凭据，回复**该**带数字
LEGIT = [
    ("对方妹妹林小雨三岁", "我妹妹多大了", ["3", "三"]),
    ("现在：2026-10-21 20:30（星期三）", "现在几点", ["20:30", "20"]),
]


def main() -> int:
    api = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    comp = Companion(api_url=api, use_memory=False, session="attr",
                     no_extract_service=True)
    print(f"编属性检测　A 组 {len(NEUTRAL)} 题 × {RUNS} 次　模型 "
          f"{os.environ.get('EXTRACT_MODEL_TAG', api)}")
    print("=" * 92)

    bad = total = 0
    samples = []
    for q in NEUTRAL:
        for _ in range(RUNS):
            comp.generator.history = []
            try:
                r = comp.say(q)
            except Exception as exc:
                r = f"（出错 {exc}）"
            r = str((r[0] if isinstance(r, tuple) else r) or "")
            total += 1
            # A 组没有记忆/工具，evidence 只有用户原话 + 时间串
            from engine.generate import now_line
            ev = (q or "") + "\n" + now_line()
            got = unsupported_numbers(ev, r)
            if got:
                bad += 1
                if len(samples) < 6:
                    samples.append((q, got, r[:80]))
    print(f"**A 组（无凭据）编属性 {bad}/{total}（{100*bad/max(1,total):.0f}%）**　← 目标 0")
    for q, got, r in samples:
        print(f"    {q} → 多出数字 {got}")
        print(f"       {r}")
    print()

    # B 组：有凭据，回复**该**带数字，不能被新守卫压掉
    #
    # ⚠ 第一版这里有个**探针 bug**：沿用 A 组那个 `use_memory=False` 的 Companion，
    #   于是记忆根本没注入、块是空的，模型当然说不出"三岁"——
    #   我把这个当成"守卫误伤"，白白怀疑了一次。**每组得用对应的构造**。
    ok = tot2 = 0
    for fact, q, wants in LEGIT:
        d = pathlib.Path(os.environ.get("TEMP", ".")) / "attr_probe"
        d.mkdir(parents=True, exist_ok=True)
        db = d / f"m_{abs(hash(q)) % 9999}.db"
        for old in (db, db.with_suffix(".db-wal"), db.with_suffix(".db-shm")):
            try:
                old.unlink()
            except Exception:
                pass
        from cognition.memory import Memory
        mem = Memory(str(db))
        ep = mem.add_episode(user_text=fact.replace("对方", "我"),
                             reply_text="嗯", summary=fact[:10],
                             session="attr", turn=1)
        mem.upsert_semantic(kind="fact", content=fact, confidence=0.8,
                            episodic_id=ep)
        mem.close()
        # ✅ **每组新建 Companion，并且用 use_memory=True + 同一个库**
        c2 = Companion(api_url=api, use_memory=True, session="attr",
                       no_extract_service=True,
                       memory_path=str(db),
                       index_path=str(d / "i.json"))
        for _ in range(RUNS):
            c2.generator.history = []
            try:
                r = c2.say(q)
            except Exception:
                r = ""
            r = str((r[0] if isinstance(r, tuple) else r) or "")
            tot2 += 1
            if any(w in r for w in wants):
                ok += 1
    print(f"B 组（有凭据·该带数字）正确 {ok}/{tot2}　← 不能被压掉")
    print()
    print("判读：")
    print("  · A 组越低越好（0 是目标）；B 组必须保持高，否则是『把数字全禁了』")
    print("  · 只有 A 降、B 不降才算真修好")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
