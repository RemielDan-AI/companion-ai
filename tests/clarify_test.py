"""钉住「不确定的记忆 → 提问 → 用户回答 → 关联回那条记忆」这条闭环。

## 为什么是这个设计（2026-10-18）

前面两次"把答案替模型算好"都输了（spotlight 42%、拟答案 49%）。
共同毛病是**替用户下结论**：记忆本来不确定，替它下结论 →
模型要么照着说错，要么干脆否认（"你没说过"）。

换成**以不确定的口吻问出去**：
    「我记得你好像提过「上周从楼下那家便利店买了花生」，是这么回事吗？」

两条好处：
    ① 不确定的提问**不会触发"你没说过"**
    ② **用户的回答本身就是数据** → 确认就升为确定、否认就否掉
       **不确定性自己消失**，不用靠模型猜

## ⚠ 本文件重点钉的两处风险

**① 同轮自问自答**
提问发生在 `assemble()`（生成**之前**），解决发生在 `remember_turn()`
（生成**之后**）——**同一轮**。不排除本轮的话，会把"触发提问的那句话"
当成"用户对这个提问的回答"，于是每条待确认记忆都被立刻误判。
→ 只认 `ask_turn < turn`。

**② 顺从偏差**
拿一个错的猜想去问，用户随口"嗯"一声，就把**错记忆升成了确定**
——比不问更糟（噪声被固化成事实）。
→ 判据从紧：否定优先（"不对"含"对"）、有转折/犹豫一律不采信、
   确认必须"剥掉肯定标记后几乎不剩什么"。**宁可漏采，不可错采。**

不连模型、不起服务。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import assemble as AS                    # noqa: E402
from cognition import clarify as C                      # noqa: E402
from cognition.memory import Memory                     # noqa: E402
from engine.retrieve import retrieve                    # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def _mem() -> tuple[Memory, int]:
    m = Memory(str(pathlib.Path(tempfile.mkdtemp()) / "m.db"))
    sid, _ = m.upsert_semantic(kind="fact", content="对方住在成都", confidence=0.53)
    return m, sid


def main() -> int:
    print("=" * 88)
    print("① 判断回答：从紧（宁可漏采，不可错采）")
    print("=" * 88)
    for u, want in (("对", "yes"), ("嗯", "yes"), ("是的", "yes"), ("嗯嗯", "yes"),
                    ("不对", "no"), ("不是", "no"), ("没说过", "no"),
                    ("不对吧", "no"), ("不记得了", "no"),
                    ("不是成都，是重庆", "corrected"),
                    ("对，但是我下个月要搬", ""),
                    ("嗯我想想", ""), ("可能吧", ""), ("好像是", ""),
                    ("今天天气不错", ""), ("我知道了", "")):
        got, why = C.judge_answer(u)
        check(got == want, f"{u!r} → {got or '不采信'}", why)
    check(C.judge_answer("不对")[0] == "no",
          "**否定优先**（「不对」含「对」，顺序反了会读成确认）")
    check(C.judge_answer("不错")[0] == "",
          "**「不错」不能被当成否认**（裸的「不」不在否定词表里）")

    print()
    print("=" * 88)
    print("② ⚠ 同轮不自问自答（提问在生成前、解决在生成后，是同一轮）")
    print("=" * 88)
    m, sid = _mem()
    m.ask_question([sid], "住处", "你好像在成都？", session="s", turn=2)
    check(C.resolve_pending(m, "对", session="s", turn=2) == {},
          "**同一轮解决 → 拒绝**（否则自问自答，每条待确认记忆都被误判）")
    r = C.resolve_pending(m, "对", session="s", turn=3)
    check(r.get("status") == "yes", "下一轮才解决", str(r.get("status")))

    print()
    print("=" * 88)
    print("③ 用户确认 → 记忆升为确定")
    print("=" * 88)
    m, sid = _mem()
    before = m.entry("semantic", sid)
    m.ask_question([sid], "住处", "你好像在成都？", session="s", turn=2)
    C.resolve_pending(m, "对的", session="s", turn=3)
    after = m.entry("semantic", sid)
    check(after["confidence"] > before["confidence"],
          "置信度上调", f"{before['confidence']:.2f} → {after['confidence']:.2f}")
    check(int(after.get("evidence_count") or 0) > int(before.get("evidence_count") or 0),
          "证据数 +1", f"{before.get('evidence_count')} → {after.get('evidence_count')}")

    print()
    print("=" * 88)
    print("④ 用户否认 → 不再注入（保留行以备审计，不删）")
    print("=" * 88)
    m, sid = _mem()
    m.ask_question([sid], "住处", "你好像在成都？", session="s", turn=2)
    C.resolve_pending(m, "不是", session="s", turn=3)
    check(m.entry("semantic", sid)["status"] == "rejected", "状态置 rejected")
    check(not any(x["id"] == sid for x in m.all_semantic(status="active")),
          "**不再进检索**")
    check(m.entry("semantic", sid) is not None, "行还在（没有删，可回溯）")

    print()
    print("=" * 88)
    print("⑤ 块里把「不确定」写成**疑问**，并登记可追踪的问题")
    print("=" * 88)
    mm = Memory(str(pathlib.Path(tempfile.mkdtemp()) / "m.db"))
    ep = mm.add_episode(user_text="花生是我从楼下那家便利店买的，那家店叫全家",
                        reply_text="哦", summary="买花生", session="s", turn=1)
    for c in ("对方上周从楼下那家便利店买了花生",
              "对方提到的人：全家（便利店）",
              "对方楼下那家便利店叫全家"):
        mm.upsert_semantic(kind="fact", content=c, confidence=0.7, episodic_id=ep)
    out = AS.assemble("我平时在哪买花生", mm,
                      retrieve("我平时在哪买花生", mm, record_access=False),
                      session="s", turn=2)
    text = out["text"]
    check("不要说得像确定的" not in text,
          "**不再出现「不要说得像确定的」**（那句话是在教模型否认）")
    check("可以顺口问一下" in text, "改成引导它**去问**，而不是禁止它说",
          text.splitlines()[-1][:52])
    asked = out.get("asked") or {}
    check(bool(asked.get("text")), "**登记了待确认的问题**", str(asked.get("text"))[:56])
    check("是这么回事吗" in str(asked.get("text")),
          "**槽位认不出时走通用回引**（引用那条记忆去问）")
    check(len(mm.all_questions()) == 1, "问题落库了（可回溯）")

    print()
    print("=" * 88)
    print("⑥ 不叠问 + 过期")
    print("=" * 88)
    m2 = Memory(str(pathlib.Path(tempfile.mkdtemp()) / "m.db"))
    sid2, _ = m2.upsert_semantic(kind="fact", content="对方喜欢喝拿铁", confidence=0.55)
    m2.ask_question([sid2], "喜好", "你喜欢拿铁？", session="s", turn=1)
    out2 = AS.assemble("我喜欢喝什么", m2,
                       retrieve("我喜欢喝什么", m2, record_access=False),
                       session="s", turn=2)
    check((out2.get("asked") or {}) == {}, "**已有 open 问题就不再问**（免得一串问题轰用户）")
    for t in range(3, 12):
        m2.ask_question([sid2], "喜好", f"第{t}问", session="s", turn=t)
    old = len([q for q in m2.all_questions(30) if q["status"] == "open"])
    n = m2.expire_questions(keep_open=6, session="s")
    new = len([q for q in m2.all_questions(30) if q["status"] == "open"])
    check(n > 0 and new < old, "**旧的 open 会过期**（防隔很多轮的一句「嗯」被误认）",
          f"open {old} → {new}")

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
