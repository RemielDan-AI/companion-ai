"""**确定性组装指标**：不测"模型答没答对"，测"该进块的东西进没进、排在第几"。

## 为什么要换指标（2026-10-16 的结论）

上限探针是**端到端**指标：它把"检索 + 组装 + 4B 生成"揉成一个分数。
实测发现：**同一批题、同一配置、不同批次的总分就能差 10 个点以上**
（逐探针会翻类别）。于是第④轮改了三处，得到 42 / 58 / 53 / 61 —— 
**5 个点以内的差异，3 次重复根本分辨不出来**，"哪个更好"这个问题
当时的测量精度答不了。

**但组装层的产出是完全确定的**：给定（库内容 + 查询），块就是一定的，
跟采样没关系。所以**程序侧的改动就该用程序侧的指标量**——
这条指标是**逐条断言**的，跑一次几秒钟，不受随机性影响。

## 量四件事

    recall    该进块的，进了没有（缺哪些）
    purity    不该进块的，有没有混进来（别人的事、低置信的）
    budget    预算内有没有被截掉
    order     该排前面的排第几（弱模型只看开头）

## 与端到端探针的关系

**两个都要**，分工不同：
    · 本指标：**程序侧改动的回归线**——快、确定、能定位
    · 上限探针：**最终验收**——它才知道"组装好了模型用不用得上"
本指标全绿**不等于**上限涨了（第④轮的 spotlight 就是反例：
块里该有的都有，但它把错的顶到了前面）。

用法：
    python tests/analysis/assemble_metric.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import alias as AL                      # noqa: E402
from cognition import assemble as AS                   # noqa: E402
from cognition.memory import Memory                    # noqa: E402
from engine.retrieve import retrieve                   # noqa: E402


def _mem(seed, aliases=(), episodes=()) -> Memory:
    """按 seed 直接建库——**不经过模型**，所以每次跑出来一模一样。"""
    d = pathlib.Path(tempfile.mkdtemp())
    m = Memory(str(d / "m.db"))
    ep_ids = []
    for u in episodes:
        ep_ids.append(m.add_episode(user_text=u, reply_text="嗯", summary=u[:18],
                                    session="s", turn=len(ep_ids) + 1))
    default_ep = ep_ids[0] if ep_ids else 0
    for item in seed:
        m.upsert_semantic(kind=item.get("kind", "fact"),
                          content=item["content"],
                          confidence=item.get("confidence", 0.7),
                          episodic_id=item.get("episodic_id", default_ep))
    for a, c in aliases:
        AL.add_alias(m, a, c, source="test")
    return m


CASES = [
    dict(
        name="单跳：妹妹的忌口",
        seed=[dict(kind="relation", content="对方提到的人：林小雨（妹妹）"),
              dict(content="对方妹妹对花生过敏")],
        episodes=["我妹妹对花生过敏"],
        query="我妹妹对什么过敏",
        must_have=["花生"],
        must_not=["海鲜", "芒果"],
    ),
    dict(
        name="别名：问「小雨」要能落到「林小雨」",
        seed=[dict(kind="relation", content="对方提到的人：林小雨（妹妹）"),
              dict(content="对方林小雨对花生过敏")],
        aliases=[("小雨", "林小雨")],
        episodes=["林小雨对花生过敏"],
        query="小雨对什么过敏",
        must_have=["花生"],
        must_not=[],
    ),
    dict(
        name="三跳：花生→便利店→全家（靠词共现那条通道）",
        seed=[dict(content="对方上周从楼下那家便利店买了花生"),
              dict(kind="relation", content="对方提到的人：全家（便利店）"),
              dict(content="对方楼下那家便利店叫全家")],
        episodes=["花生是我从楼下那家便利店买的，那家店叫全家"],
        query="我平时在哪买花生",
        must_have=["全家"],
        must_not=[],
    ),
    dict(
        name="槽位落空：字面零重合时按槽位扫（「住哪」↔「搬到成都」）",
        seed=[dict(content="对方搬到成都", episodic_id=2),
              dict(content="对方住在南京", episodic_id=1)],
        episodes=["我住在南京", "我已经搬到成都了"],
        query="我现在住哪",
        must_have=["成都"],
        must_not=[],
    ),
    dict(
        name="冲突：新旧都摆出来 + 给出处置",
        seed=[dict(content="对方搬到成都", episodic_id=2),
              dict(content="对方住在南京", episodic_id=1)],
        episodes=["我住在南京", "我已经搬到成都了"],
        query="我现在住哪",
        must_have=["南京", "成都", "新的"],
        must_not=[],
    ),
    dict(
        name="计数：三个忌口要收全（全量扫描，不走 top-k）",
        seed=[dict(kind="relation", content="对方提到的人：林小雨（妹妹）"),
              dict(content="对方林小雨对花生过敏"),
              dict(content="对方林小雨对海鲜过敏"),
              dict(content="对方林小雨不吃香菜")],
        episodes=["我妹妹林小雨对花生过敏，她对海鲜也过敏，还不吃香菜"],
        query="我妹妹有几个忌口",
        must_have=["3", "花生", "海鲜", "香菜"],
        must_not=[],
    ),
    dict(
        name="多主体隔离：问妹妹**不该**带出同事的海鲜",
        seed=[dict(kind="relation", content="对方提到的人：林小雨（妹妹）"),
              dict(kind="relation", content="对方提到的人：陈思远（同事）"),
              dict(content="对方妹妹对花生过敏"),
              dict(content="对方同事陈思远对海鲜过敏")],
        episodes=["我妹妹对花生过敏", "我同事陈思远对海鲜过敏"],
        query="我妹妹对什么过敏",
        must_have=["花生"],
        must_not=["海鲜"],
    ),
    dict(
        name="预算：期望项必须在预算内活下来",
        seed=[dict(kind="relation", content="对方提到的人：林小雨（妹妹）"),
              dict(content="对方妹妹对花生过敏")]
             + [dict(content=f"对方不相干的第{i}条琐事内容") for i in range(1, 9)],
        episodes=["我妹妹对花生过敏"],
        query="我妹妹对什么过敏",
        must_have=["花生"],
        must_not=[],
        check_budget=True,
    ),
]


def main() -> int:
    print("确定性组装指标（不连模型、不起服务、结果可复现）")
    print("=" * 96)
    tot = {"recall": [0, 0], "purity": [0, 0], "order": [0, 0], "budget": [0, 0]}
    fails: list[str] = []

    for cs in CASES:
        m = _mem(cs["seed"], cs.get("aliases", ()), cs.get("episodes", ()))
        res = retrieve(cs["query"], m, record_access=False)
        out = AS.assemble(cs["query"], m, res)
        text = out["text"]
        body = "\n".join(l for l in text.splitlines() if not l.startswith("（"))

        miss = [w for w in cs["must_have"] if w not in text]
        dirty = [w for w in cs["must_not"] if w in text]
        tot["recall"][1] += 1
        tot["recall"][0] += (not miss)
        tot["purity"][1] += 1
        tot["purity"][0] += (not dirty)

        # 顺序：第一条 must_have 在正文里排第几（1 起）
        rank = ""
        if cs["must_have"]:
            key = cs["must_have"][0]
            lines = [l for l in body.splitlines() if l.strip()]
            pos = next((i for i, l in enumerate(lines, 1) if key in l), 0)
            tot["order"][1] += 1
            tot["order"][0] += bool(pos)
            rank = f"第{pos}行" if pos else "**不在**"

        bud = ""
        if cs.get("check_budget"):
            ok = out["stats"]["chars"] <= out["stats"]["budget"] and not miss
            tot["budget"][1] += 1
            tot["budget"][0] += ok
            bud = f"　预算 {out['stats']['chars']}/{out['stats']['budget']}" \
                  f" {'✅' if ok else '✗ 被截'}"

        ok = not miss and not dirty
        print(f"\n{'✅' if ok else '✗'} {cs['name']}")
        print(f"   问：{cs['query']}")
        print(f"   召回：{'全中' if not miss else f'**缺 {miss}**'}"
              f"　纯净：{'干净' if not dirty else f'**混进 {dirty}**'}"
              f"　首项位置：{rank}{bud}")
        if not ok or os.environ.get("METRIC_VERBOSE") == "1":
            for l in body.splitlines():
                print(f"      | {l[:110]}")
        if not ok:
            fails.append(cs["name"])

    print()
    print("=" * 96)
    print(f"  召回（该进的都进了）   {tot['recall'][0]}/{tot['recall'][1]}")
    print(f"  纯净（不该进没混进来） {tot['purity'][0]}/{tot['purity'][1]}")
    print(f"  首项位置（排在预算内） {tot['order'][0]}/{tot['order'][1]}")
    if tot["budget"][1]:
        print(f"  预算未截断             {tot['budget'][0]}/{tot['budget'][1]}")
    print()
    if fails:
        print(f"✗ 不合格：{fails}")
        return 1
    print("✅ 全部合格")
    print()
    print("⚠ 全绿**不等于**上限涨了（第④轮的 spotlight 就是反例：")
    print("   块里该有的都有，但它把错的顶到了最前面）。最终验收仍要跑上限探针。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

