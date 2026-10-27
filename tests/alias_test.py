"""钉住「别名表」：简称归并 + 护栏 + 查询扩展。

## 为什么要有别名表

`林小雨（妹妹）` 这种形态只能让「妹妹」跳到「林小雨」；
用户换个说法叫「小雨」就认不出来了
（实测：三句互补说法里，问「小雨是哪个学校的」完全召不回来——
存的是「林小雨」、问的是「小雨」，词面对不上）。

## 这里最容易漏的一条（我真踩了）

**BM25 缓存的键必须含 `known`（已知专名集）**。
别名存在**另一张表**（`alias`），登记别名**不会改语义内容摘要** →
缓存不失效 → 拿到的还是别名登记前的索引 → 别名带来的分数全失效。
症状很阴：**换个进程跑就正常**，同进程里「先检索一次再登记别名」才复现。
本测试第 ⑥ 组专门钉住它。

不连模型、不起服务。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import alias as AL                      # noqa: E402
from cognition.memory import Memory                    # noqa: E402
from engine.retrieve import (expand_query, known_names,  # noqa: E402
                             retrieve)

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def fresh():
    d = pathlib.Path(tempfile.mkdtemp())
    return Memory(str(d / "m.db"))


def main() -> int:
    print("=" * 80)
    print("① 护栏：别名错了比没有更糟（会把两个人的事混在一起）")
    print("=" * 80)
    m = fresh()
    for alias, canonical, want, why in (
        ("小雨", "林小雨", "added", "正常简称"),
        ("小雨", "林小雨", "exists", "重复登记"),
        ("小雨", "陈小雨", "rejected", "**歧义**：已指向别人，绝不覆盖"),
        ("先生", "王先生", "rejected", "泛称/类型词"),
        ("老师", "李老师", "rejected", "泛称/类型词"),
        ("雨", "林小雨", "rejected", "简称太短"),
        ("林小雨", "小雨", "rejected", "简称不比正名短"),
        ("小雨", "小雨", "rejected", "与正名相同"),
    ):
        got = AL.add_alias(m, alias, canonical, source="test")
        check(got["action"] == want, why, f"{alias}→{canonical} = {got['action']}"
              + (f"（{got['reason']}）" if got["reason"] else ""))

    print()
    print("=" * 80)
    print("② 人工否掉的不能复活（否掉要算数）")
    print("=" * 80)
    m2 = fresh()
    AL.add_alias(m2, "小雨", "林小雨", source="test")
    AL.reject_alias(m2, "小雨", "林小雨", note="不是同一个人")
    check(AL.canonical_of(m2, "小雨") == "小雨", "否掉后不再当别名")
    again = AL.add_alias(m2, "小雨", "林小雨", source="test")
    check(again["action"] == "rejected", "再登记被拒", again["reason"])

    print()
    print("=" * 80)
    print("③ 学习规则：**简称必须真的出现在用户原话里**（否则是纯猜）")
    print("=" * 80)
    m3 = fresh()
    got = AL.learn_from_turn(m3, "小雨是广州大学的学生", relations=[],
                             known={"林小雨"})
    check(len(got) == 1 and got[0]["alias"] == "小雨", "原话出现「小雨」→ 学到",
          str([(g["alias"], g["canonical"]) for g in got]))
    none = AL.learn_from_turn(m3, "我妹妹在读大学", relations=[], known={"林小雨"})
    check(not none, "原话没出现「小雨」→ 不学（纯猜要挡住）")
    only_full = AL.learn_from_turn(m3, "林小雨昨天来了", relations=[],
                                   known={"林小雨"})
    check(not only_full, "只出现全名、没有简称 → 不学")
    other = AL.learn_from_turn(m3, "思远最近很忙", relations=[], known={"陈思远"})
    check(any(g["alias"] == "思远" for g in other), "另一个人的简称也能学")

    print()
    print("=" * 80)
    print("④ 回填：简称先到、全名后到（入库那刻学不到，得回头补）")
    print("=" * 80)
    m4 = fresh()
    m4.add_episode(user_text="小雨是广州大学的学生", reply_text="嗯",
                   summary="", session="t", turn=1)
    check(not AL.all_aliases(m4), "只有简称时学不出别名（还不知道全名）")
    m4.upsert_semantic(kind="relation", content="对方提到的人：林小雨（妹妹）",
                       confidence=0.7)
    res = AL.backfill(m4)
    rows = AL.all_aliases(m4)
    check(res["action"] == "done" and rows, "回填补上了", str(
        [(r["alias"], r["canonical"]) for r in rows]))
    check(all("：" not in r["canonical"] and not r["canonical"].startswith("对方")
              for r in rows), "**正名不带前缀**（实测踩过：`小雨→对方提到的人：林小雨`）",
          str([r["canonical"] for r in rows]))

    print()
    print("=" * 80)
    print("④b **整句不能当正名**（真踩过：别名表混进 `上学→妹妹在广州上学`）")
    print("=" * 80)
    m4b = fresh()
    for content in ("对方提到的人：林小雨（妹妹）", "对方妹妹在广州上学",
                    "对方妹妹名叫林小雨", "对方就读于广州大学"):
        m4b.upsert_semantic(kind="fact", content=content, confidence=0.7)
    names = AL.full_names(m4b)
    check(names == {"林小雨"}, "`full_names` 只收真专名，不收整句", str(sorted(names)))
    m4b.add_episode(user_text="小雨是广州大学的学生", reply_text="嗯",
                    summary="", session="t", turn=1)
    AL.backfill(m4b)
    got_rows = [(r["alias"], r["canonical"]) for r in AL.all_aliases(m4b)]
    check(got_rows == [("小雨", "林小雨")], "回填只产出真正的人名简称", str(got_rows))

    print()
    print("=" * 80)
    print("④c **子串不算证据**（真踩过：`州大学 → 广州大学`）")
    print("=" * 80)
    m4c = fresh()
    got = AL.learn_from_turn(m4c, "我在广州大学上学", relations=[],
                             known={"广州大学"})
    check(not got, "候选是全名的子串、只是被全名带出来的 → 不学",
          str([(g["alias"], g["canonical"]) for g in got]))
    got2 = AL.learn_from_turn(m4c, "广州大学挺好的，广大我也去过", relations=[],
                              known={"广州大学"})
    check(all(g["alias"] != "州大学" for g in got2),
          "仍然不许出现「州大学」这种子串别名",
          str([(g["alias"], g["canonical"]) for g in got2]))

    print()
    print("=" * 80)
    print("⑤ 查询扩展：问「小雨」要能搜到「林小雨」的 facts")
    print("=" * 80)
    m5 = fresh()
    m5.upsert_semantic(kind="relation", content="对方提到的人：林小雨（妹妹）",
                       confidence=0.7)
    m5.upsert_semantic(kind="fact", content="对方林小雨不能吃花生", confidence=0.7)
    check("小雨" not in known_names(m5), "别名登记前「小雨」不是已知专名")
    AL.add_alias(m5, "小雨", "林小雨", source="test")
    check("小雨" in known_names(m5), "别名登记后「小雨」成了已知专名")
    check("林小雨" in expand_query("小雨是哪个学校的", m5),
          "查询扩展把正名拼了进去", expand_query("小雨是哪个学校的", m5))
    r = retrieve("小雨是哪个学校的", m5, record_access=False)
    got = [i["content"] for i in r["semantic"]]
    check(any("花生" in c for c in got), "**用简称问，召回了正名上的事实**", str(got))

    print()
    print("=" * 80)
    print("⑥ 缓存回归：别名存在别的表里，登记别名**不改语义摘要** → 缓存必须失效")
    print("=" * 80)
    m6 = fresh()
    m6.upsert_semantic(kind="relation", content="对方提到的人：林小雨（妹妹）",
                       confidence=0.7)
    m6.upsert_semantic(kind="fact", content="对方林小雨不能吃花生", confidence=0.7)
    before = retrieve("小雨是哪个学校的", m6, record_access=False)
    check(not before["semantic"], "登记前召不回（此时还不知道小雨是谁）")
    AL.add_alias(m6, "小雨", "林小雨", source="test")
    after = retrieve("小雨是哪个学校的", m6, record_access=False)
    check(any("花生" in i["content"] for i in after["semantic"]),
          "**同进程内**登记后立刻生效（缓存键含 known）",
          str([i["content"] for i in after["semantic"]]))

    print()
    print("=" * 80)
    print("⑧ 模型提议 + 程序裁决：**模型只做闭集选择，写入由程序定**")
    print("=" * 80)
    import os

    class Stub:
        def __init__(self, answer):
            self.answer = answer

        def complete(self, *a, **k):
            return self.answer

    def ask(answer, text, rels, known_rows):
        d = pathlib.Path(tempfile.mkdtemp())
        mm = Memory(str(d / "m.db"))
        for name, rel in known_rows:
            mm.upsert_semantic(kind="relation",
                               content=f"对方提到的人：{name}（{rel}）",
                               confidence=0.7)
        mm.add_episode(user_text=text, reply_text="嗯", summary="",
                       session="t", turn=1)
        AL.model_propose_aliases(mm, Stub(answer), text, relations=rels)
        return [(r["alias"], r["canonical"]) for r in AL.all_aliases(mm)]

    # ① 闭集：模型编造一个名单外的人 → 收成空，不写
    got = ask("张三", "小雨最近怎样", ["对方提到的人：小雨（学生）"],
              [("林小雨", "妹妹")])
    check(got == [], "**模型点名一个不存在的人 → 不写**", str(got))
    # ② 模型答「新」→ 不写
    got = ask("新", "我同事叫王海涛", ["对方提到的人：王海涛（同事）"],
              [("林小雨", "妹妹")])
    check(got == [], "模型答「新」→ 不写", str(got))
    # ③ 闭集内命中 → 写（带前后缀也能收成名字）
    got = ask("是林小雨。", "小雨最近怎样", ["对方提到的人：小雨（学生）"],
              [("林小雨", "妹妹")])
    check(got == [("小雨", "林小雨")], "闭集内命中 → 写", str(got))
    # ④ ⚠ 类型硬拦：猫不能指认成人（真踩过，提示有诱导性时 2B 会答错）
    got = ask("林小雨", "我养了只猫叫团子", ["对方提到的人：团子（宠物）"],
              [("林小雨", "妹妹")])
    check(got == [], "**类型不符（宠物 vs 人）→ 硬拦不写**", str(got))
    # ⑤ 类型一致才放行
    got = ask("陈思远", "思远昨天来了", ["对方提到的人：思远（同事）"],
              [("陈思远", "同事")])
    check(got == [("思远", "陈思远")], "同为「人」→ 放行", str(got))
    # ⑥ 关掉开关就不问模型
    os.environ["COMPANION_NO_ALIAS_MODEL"] = "1"
    try:
        got = ask("林小雨", "小雨最近怎样", ["对方提到的人：小雨（学生）"],
                  [("林小雨", "妹妹")])
        check(got == [], "COMPANION_NO_ALIAS_MODEL=1 时不写")
    finally:
        os.environ.pop("COMPANION_NO_ALIAS_MODEL", None)

    print()
    print("=" * 80)
    print("⑦ 接线：表/学习/回填都在（防以后被摘掉）")
    print("=" * 80)
    cols = [r[1] for r in m.conn.execute("PRAGMA table_info(alias)")]
    for col in ("alias", "canonical", "source", "status", "confidence"):
        check(col in cols, f"alias 表有 `{col}` 列")
    ex = (ROOT / "cognition" / "extract.py").read_text(encoding="utf-8")
    check("learn_from_turn" in ex, "入库时会学别名")
    ri = (ROOT / "cognition" / "reindex.py").read_text(encoding="utf-8")
    check("backfill_alias" in ri, "启动后台线程会回填别名")
    rd = (ROOT / "engine" / "retrieve.py").read_text(encoding="utf-8")
    check("expand_query" in rd, "检索里调了查询扩展")
    check("_digest(sorted(str(x) for x in (known or set())))" in rd,
          "BM25 缓存键里含 known（第⑥组那个 bug 的修法）")

    print()
    print("=" * 80)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
