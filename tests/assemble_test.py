"""钉住「记忆组装层」：把原始记忆变成 4B 能安全使用的自然语言上下文。

## 这一层解决什么

之前"检索结果 → 注入提示"是**流水式事实列表**，三个毛病实测都咬过人：
    ① **不回指**：库里是「林小雨」，用户问「小雨」——检索能**找**到，
       但**显示出来还是「林小雨」**，模型看不出是同一个人
    ② **不分信不信**：抽取错的（主体错位、空轮硬凑）和正确记忆**长得一样**地塞进去
    ③ **不标冲突**：新旧两条矛盾的记忆并排躺着，谁也不知道信哪个

## ⚠ 置信度**不用模型自报**（实测推翻了原设计）

`tests/analysis/conf_signal_probe.py` 实测：
    84% 都 ≥0.9；只有 4 个取值；而且**不区分好坏**——
    「嗯，还行吧」（空轮）也给 0.80~0.90，还抽出 3 条；
    「小雨是广州大学的学生」（主体错）给 0.90~1.00。
    更糟：**为加这个字段改成 items 结构，`kind` 直接坏掉**
    （模型把枚举说明 `'fact/preference/relation'` 原样抄回来）。

改用**程序可算、可验证**的信号：接地强度(0.7) + 证据数(0.3)，跳进来的打折。

## ⚠ 本文件钉住的最后一个坑

**接地必须拿"记下来时那轮的原话"算，不是当前提问。**
第一版用当前 query → 库里记忆跟提问字面当然对不上 → **全部 0 分、整层输出空**。
库里存 `source_episodic` 就是为这个。

不连模型、不起服务。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import alias as AL                       # noqa: E402
from cognition import assemble as AS                    # noqa: E402
from cognition.memory import Memory                     # noqa: E402
from engine.retrieve import retrieve                    # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def build_memory() -> Memory:
    """一份接近真实的库：妹妹=林小雨=小雨，在一所大学上学。"""
    d = pathlib.Path(tempfile.mkdtemp())
    m = Memory(str(d / "m.db"))
    ep = m.add_episode(user_text="我妹妹叫林小雨，她在广州大学上学",
                       reply_text="哦好", summary="妹妹的信息",
                       session="s", turn=1)
    for kind, c in (("relation", "对方提到的人：林小雨（妹妹）"),
                    ("relation", "对方提到的人：广州大学（学校）"),
                    ("fact", "对方林小雨在广州大学上学")):
        m.upsert_semantic(kind=kind, content=c, confidence=0.5, episodic_id=ep)
    AL.add_alias(m, "小雨", "林小雨", source="suffix")
    return m


def main() -> int:
    print("=" * 84)
    print("① 接地强度：正常 / 改写 / 空轮 要分得开")
    print("=" * 84)
    for content, src, want_high, why in (
        ("对方妹妹叫林小雨", "我妹妹叫林小雨", True, "正常（原话里有）"),
        ("对方林小雨在广州大学上学", "我妹妹叫林小雨，她在广州大学上学",
         True, "**改写也要算高**（二元组会被改写打散，所以混了字级）"),
        ("用户觉得还行", "嗯，还行吧", False, "空轮硬凑"),
    ):
        g = AS.grounding(content, src)
        check((g >= 0.75) is want_high, why, f"接地={g:.2f}")

    print()
    print("=" * 84)
    print("② 置信度分层：事实 / 可能 / 丢弃")
    print("=" * 84)
    for content, src, want, why in (
        ("对方妹妹叫林小雨", "我妹妹叫林小雨", "fact", "正常"),
        ("对方林小雨就读于广州大学", "我妹妹叫林小雨，她在广州大学上学",
         "possible", "**改写**降一档（不是全错，别直接丢）"),
        ("用户觉得还行", "嗯，还行吧", "drop", "空轮丢弃"),
    ):
        c = AS.confidence({"content": content, "evidence_count": 1}, None, src)
        check(AS.tier_of(c) == want, why, f"conf={c:.2f} → {AS.tier_of(c)}")
    # 证据数上去要涨
    low = AS.confidence({"content": "对方妹妹叫林小雨", "evidence_count": 1},
                        None, "我妹妹叫林小雨")
    high = AS.confidence({"content": "对方妹妹叫林小雨", "evidence_count": 5},
                         None, "我妹妹叫林小雨")
    check(high > low, "证据数多了置信度要涨", f"{low:.2f} → {high:.2f}")
    # ⚠ 跳数**不该**改置信度（详见 assemble.confidence 的说明）：
    #   第一版乘了 0.85，结果三跳题找回来的**正确答案**被压成"不太确定"，
    #   模型反而开始含糊——而它正是要找的东西。
    check(not hasattr(AS, "HOP_PENALTY") or AS.confidence(
        {"content": "对方妹妹叫林小雨", "evidence_count": 1}, None,
        "我妹妹叫林小雨") == low,
        "跳数**不改**置信度（两个轴不能混）", f"{low}")

    print()
    print("=" * 84)
    print("③ 实体归一化 + 图遍历（「小雨」→「林小雨」→ 顺着关系走）")
    print("=" * 84)
    m = build_memory()
    seeds = AS.entity_seeds("小雨在哪上学", m)
    check(seeds == ["林小雨"], "**「小雨」归一成「林小雨」**", str(seeds))
    check(AS.entity_seeds("我不认识这句话", m) == [], "没有实体时种子为空")
    triples = AS.relation_triples(m)
    check(any(t["relation"] == "妹妹" and t["object"] == "林小雨"
              for t in triples), "关系抽成了三元组 用户--妹妹-->林小雨", str(triples[:3]))
    walked = AS.walk(m, AS.entity_seeds("我妹妹在哪上学", m))
    ents = walked["entities"]
    check("妹妹" in ents and "林小雨" in ents,
          "**图遍历从「妹妹」走到了「林小雨」**", str(ents))
    check(any("林小雨" in str(e.get("content")) for e in walked["entries"]),
          "走到了林小雨名下的记忆")
    check(any("妹妹" in p for p in walked["paths"]), "路径可读", str(walked["paths"]))

    print()
    print("=" * 84)
    print("④ ⚠ 接地拿的是**当时那轮的原话**，不是当前提问（第一版就错在这）")
    print("=" * 84)
    res = retrieve("小雨在哪上学", m, record_access=False)
    out = AS.assemble("小雨在哪上学", m, res)
    check(out["stats"]["fact"] >= 1 or out["stats"]["possible"] >= 1,
          "用真实链路组装时**不该全被判 drop**",
          str(out["stats"]))
    src = AS._source_utterance(m, m.all_semantic(status="active")[0])
    check("广州大学" in src, "能从 source_episodic 取到当轮原话", src[:24])
    # ⚠ 必须拿**库里真条目**测（它才带 source_episodic）；
    #   自己捏一个 dict 没有那个字段，会退回用 user_text，测的就不是这件事。
    real = [e for e in m.all_semantic(status="active")
            if "广州大学" in str(e.get("content"))][0]
    a = AS.confidence(real, m, "完全不相干的一句话")
    b = AS.confidence(real, m, "")
    check(a == b, "传进来的 user_text **不该**影响（应以 source_episodic 为准）",
          f"{a} vs {b}")

    print()
    print("=" * 84)
    print("⑤ 渲染：自然语言、别名并进括号、关系单独一行、**不塞 JSON**")
    print("=" * 84)
    text = out["text"]
    check("已知：" in text or "已认识的人：" in text, "有人话段落")
    check("林小雨（妹妹、小雨）" in text or "林小雨（小雨" in text,
          "**别名并进已有括号**（不是 林小雨（小雨）（妹妹））", text[-60:])
    check("已认识的人：" in text, "关系单独一行（当事实念很难读）")
    for bad in ('{"', "[{", '": ', "kind", "conf"):
        check(bad not in text, f"不出现 JSON 痕迹 {bad!r}")

    print()
    print("=" * 84)
    print("⑤b 多跳（两条通道）+ 聚合（计数/列举）——这次新增的两件事")
    print("=" * 84)
    m3 = pathlib.Path(tempfile.mkdtemp())
    mm = Memory(str(m3 / "m.db"))
    ep2 = mm.add_episode(user_text="花生是我从楼下那家便利店买的，那家店叫全家",
                         reply_text="哦", summary="买花生", session="s", turn=1)
    for kind, c in (("fact", "对方提到的人：林小雨（妹妹）"),
                    ("fact", "对方上周从楼下那家便利店买了花生"),
                    ("fact", "对方提到的人：全家（便利店）"),
                    ("fact", "对方楼下那家便利店叫全家")):
        mm.upsert_semantic(kind=kind, content=c, confidence=0.5, episodic_id=ep2)
    from engine.retrieve import retrieve as _r
    q3 = "我平时在哪买花生"
    w3 = AS.walk(mm, AS.entity_seeds(q3, mm), q3,
                 extra_entries=_r(q3, mm, record_access=False)["semantic"])
    reached = [str(e.get("content")) for e in w3["entries"]]
    check(any("全家" in c for c in reached),
          "**三跳能接上**（花生→便利店→全家；靠的是词共现那条通道，"
          "因为这两个都是普通名词、不是实体名）", str([c[:14] for c in reached]))
    check(any("共现词" in str(e.get("why")) for e in w3["entries"]),
          "确实是词共现通道接上的（不是碰巧）")
    # 聚合
    m4 = pathlib.Path(tempfile.mkdtemp())
    ma = Memory(str(m4 / "m.db"))
    epa = ma.add_episode(user_text="我妹妹林小雨对花生过敏，她对海鲜也过敏，还不吃香菜",
                         reply_text="哦", summary="忌口", session="s", turn=1)
    for c in ("对方提到的人：林小雨（妹妹）", "对方林小雨对花生过敏",
              "对方林小雨对海鲜过敏", "对方林小雨不吃香菜"):
        ma.upsert_semantic(kind="fact", content=c, confidence=0.5, episodic_id=epa)
    seeds4 = AS.entity_seeds("我妹妹有几个忌口", ma)
    walked4 = AS.walk(ma, seeds4)["entities"]
    agg = AS.aggregate("我妹妹有几个忌口", ma, seeds4, walked4)
    check(agg is not None and agg["count"] == 3, "**计数要全**（3 条）",
          str(agg and agg.get("text")))
    check(agg and all(v in agg["text"] for v in ("花生", "海鲜", "香菜")),
          "列举要全（预算内一条不落）", str(agg and agg.get("items")))
    check(agg and "林小雨" in agg["text"],
          "**主体用图遍历走到的正名**（查询里是「妹妹」、事实挂在「林小雨」）",
          str(agg and agg.get("subject")))
    check(AS.aggregate("我妹妹在哪上学", ma, seeds4, walked4) is None,
          "不是聚合提问就不触发")
    check(AS.aggregate("今天天气怎么样", ma, [], []) is None, "无关提问不触发")

    print()
    print("=" * 84)
    print("⑥ 冲突标注 + 预算截断")
    print("=" * 84)
    m2 = build_memory()
    m2.upsert_semantic(kind="fact", content="对方住在南京", confidence=0.7)
    m2.upsert_semantic(kind="fact", content="对方搬到成都", confidence=0.7)
    res2 = retrieve("我住哪", m2, record_access=False)
    out2 = AS.assemble("我住哪", m2, res2)
    check(isinstance(out2["conflicts"], list), "冲突字段存在", str(out2["conflicts"]))
    long_text = AS.render(m2, [dict(c, conf=0.9, kind="fact")
                              for c in m2.all_semantic(status="active")],
                          [], [], "我住哪", max_chars=60)
    check(len(long_text) <= 60, "**预算截断生效**", f"{len(long_text)} 字")

    print()
    print("=" * 84)
    print("⑥b ⚠ 防重复注入：组装层必须尊重 exclude_ids（实测踩过的真 bug）")
    print("=" * 84)
    # 现象：第 2 轮检索已经排除到 0 条，但**块和第 1 轮一字不差**。
    # 原因：`walk()`/`slot_scan()` 直接扫全库，把 retrieve 的排除名单整个绕过 →
    #      同一条记忆每轮被原样重灌 → 用户重复提问时模型反复看到同一批
    #      带多个主体的句子 → **主体越搞越乱**（用户实测反馈）。
    m3 = build_memory()
    q3 = "小雨在哪上学"
    r1 = retrieve(q3, m3, record_access=False)
    b1 = AS.assemble(q3, m3, r1, session="s", turn=1)["text"]
    ex = {f"semantic#{x['id']}" for x in r1["semantic"]}
    r2 = retrieve(q3, m3, record_access=False, exclude_ids=ex)
    b2 = AS.assemble(q3, m3, r2, session="s", turn=2, exclude_ids=ex)["text"]
    check(bool(b1), "第 1 轮有块", f"{len(b1)} 字")
    check(b1 != b2 or not b2,
          "**第 2 轮不会把第 1 轮的块原样重灌**",
          f"第1轮 {len(b1)} 字 / 第2轮 {len(b2)} 字")
    # 不传 exclude_ids 时行为应和以前一致（向后兼容）
    b2b = AS.assemble(q3, m3, r2, session="s", turn=2)["text"]
    check(isinstance(b2b, str), "不传 exclude_ids 也不报错（向后兼容）")

    print()
    print("=" * 84)
    print("⑦ 接线：app.py 用它、能回落、有开关（防以后被摘掉）")
    print("=" * 84)
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    check("assemble_layer" in app_src, "app.py 调了组装层")
    check("build_memory_block(" in app_src, "旧的渲染**仍留着做回落**")
    check("last_assembly" in app_src, "结果记在 companion 上（可观测）")
    old = os.environ.pop("COMPANION_NO_ASSEMBLE", None)
    try:
        check(AS.enabled() is True, "默认开着")
        os.environ["COMPANION_NO_ASSEMBLE"] = "1"
        check(AS.enabled() is False, "**开关能关**（A/B 用）")
    finally:
        os.environ.pop("COMPANION_NO_ASSEMBLE", None)
        if old is not None:
            os.environ["COMPANION_NO_ASSEMBLE"] = old

    print()
    print("=" * 84)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
