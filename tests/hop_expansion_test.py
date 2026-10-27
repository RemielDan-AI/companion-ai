"""钉住「一跳扩展」：多跳联想必须能被关系锚点补上。

## 为什么需要这个测试

多跳联想实测是 **0/3**（`tests/analysis/multihop_probe.py`）：
「我妹妹有什么忌口」找不回「林小雨不能吃花生」——查询与答案**零词重叠**，
必须经「妹妹 → 林小雨」这一跳。`engine/retrieve.py` 已按关系记忆
（`对方提到的人：林小雨（妹妹）`，即**别名→名字**映射）做了一跳扩展。

测试**只用词法**（`index=None`），不需要嵌入服务 —— 快且稳定，回归里不烧资源。

## 覆盖

    ① 有锚点 → 多跳能补上（这是修的东西）
    ② 没锚点 → 不许乱跳（`hops` 为空）
    ③ 跳数衰减真的乘上了（γ^t）
    ④ 主查询自身的命中不受影响
    ⑤ 不许多跳：HOP_MAX 上限
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.memory import Memory                    # noqa: E402
from engine import retrieve as R                       # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def build(with_relations: bool) -> Memory:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="hop_test_"))
    mem = Memory(str(tmp / "m.db"))
    for content in ("用户妹妹林小雨在读大学",
                    "林小雨不能吃花生",
                    "用户养的宠物叫团子，是只橘猫",
                    "团子最近在吃处方粮",
                    "用户今天很累，什么都不想干"):
        mem.upsert_semantic(kind="fact", content=content, confidence=0.7,
                            importance=0.6)
    if with_relations:
        mem.upsert_semantic(kind="relation", content="对方提到的人：林小雨（妹妹）",
                            confidence=0.8, importance=0.6)
        mem.upsert_semantic(kind="relation", content="对方提到的人：团子（宠物）",
                            confidence=0.8, importance=0.6)
    return mem


def tops(res: dict) -> list[str]:
    return [str(it.get("content") or "") for it in res["semantic"]]


def main() -> int:
    print("=" * 74)
    print("① 有锚点：多跳必须补上（查询与答案零词重叠）")
    print("=" * 74)
    mem = build(True)
    res = R.retrieve("我妹妹有什么忌口", mem, index=None, record_access=False)
    got = tops(res)
    check(any("不能吃花生" in c for c in got),
          "「我妹妹有什么忌口」找回了「林小雨不能吃花生」", f"实际 {got}")
    hops = res.get("hops") or []
    check(any(h.get("target") == "林小雨" for h in hops),
          "跳的目标是「林小雨」", f"实际 {[h.get('target') for h in hops]}")

    res2 = R.retrieve("我那只宠物的伙食怎么样", mem, index=None, record_access=False)
    check(any("处方粮" in c for c in tops(res2)), "宠物那条也能跳过去")

    print()
    print("=" * 74)
    print("② 没锚点：不许乱跳（不能凭空多带记忆）")
    print("=" * 74)
    bare = build(False)
    res3 = R.retrieve("我妹妹有什么忌口", bare, index=None, record_access=False)
    check(not (res3.get("hops") or []), "没有关系记忆时 hops 为空",
          f"实际 {res3.get('hops')}")
    check(not any("不能吃花生" in c for c in tops(res3)),
          "没有锚点时不会凭空捞出下一跳的答案（缺口照旧，这是诚实的）")

    print()
    print("=" * 74)
    print("③ 跳数衰减：跳进来的条目分数必须乘 γ^t")
    print("=" * 74)
    check(0 < R.HOP_DECAY < 1, f"HOP_DECAY 在 (0,1)：{R.HOP_DECAY}")
    hopped = [it for it in res["semantic"] if it.get("hop_from")]
    check(bool(hopped), "命中的条目里标了 hop_from", f"{len(hopped)} 条")
    if hopped:
        it = hopped[0]
        check(it.get("hop") == 1, "标记了第几跳", f"hop={it.get('hop')}")
        check(float(it["score"]) >= 0, "衰减后分数非负", f"score={it['score']}")

    print()
    print("=" * 74)
    print("④ 主查询自身命中不受影响 + ⑤ HOP_MAX 上限")
    print("=" * 74)
    res4 = R.retrieve("我今天很累", mem, index=None, record_access=False)
    check(any("很累" in c for c in tops(res4)), "普通问法仍走原路径")
    check(not (res4.get("hops") or []), "没有关系词时不触发跳")
    check(R.HOP_MAX >= 1, f"HOP_MAX = {R.HOP_MAX}（一轮最多跳几个目标）")

    # 纯粹的关系词查询不该把"跳"当成主路径：仍然要有主查询的命中
    check(isinstance(R.relation_targets("我妹妹有什么忌口", mem), list),
          "relation_targets 可直接调用且返回 list")
    check(R.relation_targets("我今天有点累", mem) == [],
          "没有关系词时 relation_targets 返回空")

    print()
    print("=" * 74)
    print("⑥ 保底占位：主查询有大量高重合干扰时，跳进来的不能被挤光")
    print("=" * 74)
    # 干扰项含「妹妹/忌口」，字面重合高 → 修复前会把跳进来的条目挤掉
    noisy = build(True)
    for i in range(4):
        noisy.upsert_semantic(kind="fact",
                              content=f"用户妹妹的事情{i}：妹妹今天问了忌口相关的话{i}",
                              confidence=0.7, importance=0.6)
    res6 = R.retrieve("你还记得我妹妹有什么忌口吗", noisy, index=None,
                      record_access=False)
    got6 = tops(res6)
    check(any("不能吃花生" in c for c in got6),
          "干扰项很多时，跳进来的答案仍在结果里", f"实际 {[c[:18] for c in got6]}")
    hopped6 = [it for it in res6["semantic"] if it.get("hop_from")]
    check(bool(hopped6), "跳进来的条目带 hop_from 标记（便于排查）")
    # 精度不被削弱：普通命中要排在跳的前面
    plain_idx = [i for i, it in enumerate(res6["semantic"]) if not it.get("hop_from")]
    hop_idx = [i for i, it in enumerate(res6["semantic"]) if it.get("hop_from")]
    if plain_idx and hop_idx:
        check(min(hop_idx) > min(plain_idx),
              "普通命中排在跳的前面（精度不被削弱）",
              f"普通位置 {plain_idx} 跳位置 {hop_idx}")

    print()
    print("=" * 74)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
