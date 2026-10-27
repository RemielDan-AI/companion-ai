"""检索质量标尺：固定问题 → 看目标记忆有没有被检索到、排第几。

## 为什么要它

要改检索（BM25 / embedding / rerank），得先能**量**。

## ⚠ 2026-10-17 重写：从 10 题扩到 40 题，并且**分类**

原来只有 10 题、不分类，于是"换嵌入模型"的实验只能看出一个总数
（9/10 → 10/10）——**一道题的差异，说明不了任何事**，
也看不出"新模型到底在**哪一类**问题上更好/更差"。

现在按**问题类型**分类，分类的意义是：不同类别**该由不同机制赢**。

    口头改写  问法与原文用词不同（"我晚上休息前有什么习惯" ↔ "睡前看半小时书"）
              → **向量该赢**（词法对不上）
    字面重合  问法里的词原文就有
              → **词法该赢**（不该被向量带偏）
    否定例外  "不吃香菜""不能喝牛奶"
              → 否定词是关键，看会不会被当成普通字
    多主体    妹妹/同事/妈妈 各有各的事
              → **不许串**（串了就是把别人的事记成用户的）
    状态变更  以前/现在两种说法都在库里
              → 看问"以前"能不能拿到旧的
    数字时间  "三岁""周三""半小时"
              → 数字是一字不差的，不该被模糊掉
    长尾专名  Obsidian / 鹤岗
              → **IDF 该起作用**（高频字组不该跟它同权）

## 报什么

    hit@4      目标有没有进前 4
    MRR        平均倒数排名（比 hit 更敏感——排第 1 和排第 4 不是一回事）
    分类小计   每一类各中多少（**这才是诊断用的**）
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import decay as decay_engine       # noqa: E402
from cognition.memory import Memory                # noqa: E402
from cognition.vectors import VectorIndex, health as embed_health  # noqa: E402
from engine import retrieve as R                   # noqa: E402

# (类别, 埋点事实, 问法, 期望命中的关键词)
CASES = [
    # ---- 口头改写：问法与原文用词对不上，**向量该赢** ----
    ("口头改写", "用户对芒果过敏", "我吃芒果会不舒服吗", "芒果"),
    ("口头改写", "用户在学吉他，只学到和弦", "我最近在练什么", "吉他"),
    ("口头改写", "用户喜欢秋天，喜欢看落叶", "我最爱什么季节", "秋天"),
    ("口头改写", "用户睡前看半小时书", "我晚上休息前有什么习惯", "书"),
    ("口头改写", "用户讨厌无议程的会议", "什么样的会议让我烦", "议程"),
    ("口头改写", "用户只喝美式咖啡，不加糖", "我平常喝什么咖啡", "美式"),
    ("口头改写", "用户已从南京搬到成都", "我目前在哪个城市", "成都"),
    ("口头改写", "对方养了一只叫团子的橘猫，三岁", "我家宠物叫什么", "团子"),
    ("口头改写", "用户有慢性胃炎，吃辣会难受", "我胃怎么样", "胃炎"),
    ("口头改写", "用户每天通勤要一个小时", "我上班路上要多久", "通勤"),
    # ---- 字面重合：原文就有这些词，**词法该赢** ----
    ("字面重合", "用户在工业软件公司上班", "我在哪上班", "软件"),
    ("字面重合", "用户妹妹林小雨在读大学", "我妹妹叫什么", "林小雨"),
    ("字面重合", "用户喜欢喝拿铁", "我喜欢喝什么", "拿铁"),
    ("字面重合", "用户养了一只叫团子的橘猫", "团子是什么动物", "橘猫"),
    ("字面重合", "用户的老家在鹤岗", "我老家在哪", "鹤岗"),
    # ---- 否定与例外 ----
    ("否定例外", "用户不吃香菜", "我有什么忌口", "香菜"),
    ("否定例外", "用户不能喝牛奶，会拉肚子", "我不能喝什么", "牛奶"),
    ("否定例外", "用户不喜欢太吵的地方，演唱会除外", "我讨厌什么样的地方", "吵"),
    ("否定例外", "用户不喝酒，但偶尔喝一点米酒", "我喝酒吗", "酒"),
    ("否定例外", "用户没去过日本", "我去过日本吗", "日本"),
    # ---- 多主体：不许串 ----
    ("多主体", "用户妹妹小满对花生过敏", "我妹妹对什么过敏", "花生"),
    ("多主体", "用户同事陈思远对海鲜过敏", "我同事对什么过敏", "海鲜"),
    ("多主体", "用户妈妈在杭州住院", "我妈在哪", "杭州"),
    ("多主体", "用户爸爸喜欢下象棋", "我爸喜欢什么", "象棋"),
    ("多主体", "对方另一只猫叫汤圆，是布偶", "汤圆是什么猫", "布偶"),
    # ---- 状态变更 ----
    ("状态变更", "用户以前住南京，现在住成都", "我以前住哪", "南京"),
    ("状态变更", "用户以前住南京，现在住成都", "我现在住哪", "成都"),
    ("状态变更", "用户从银行换到了学校工作", "我以前在哪个行业", "银行"),
    ("状态变更", "用户上个月把手机换成了安卓", "我以前用什么手机", "手机"),
    # ---- 数字与时间 ----
    ("数字时间", "用户的猫团子三岁", "我家猫多大了", "三岁"),
    ("数字时间", "用户每周三晚上去游泳", "我哪天去游泳", "周三"),
    ("数字时间", "用户每天睡七个小时", "我睡几个小时", "七个小时"),
    ("数字时间", "用户的生日是十一月十二号", "我生日几号", "十二"),
    # ---- 长尾专名：**IDF 该起作用** ----
    ("长尾专名", "用户在用 Obsidian 记笔记", "我用什么软件记笔记", "Obsidian"),
    ("长尾专名", "用户在用 Anki 背单词", "我用什么背单词", "Anki"),
    ("长尾专名", "用户的猫得了猫传腹", "我的猫得了什么病", "猫传腹"),
    ("长尾专名", "用户在用飞书做项目管理", "我用什么做项目管理", "飞书"),
]

# 干扰项：与上面问题**无关但含高频字**的句子（用来看 IDF 有没有起作用）
DISTRACTORS = [
    "我今天很累，什么都不想干",
    "我觉得这样也挺好的",
    "我是不是很没用",
    "你说我该怎么办",
    "我最近压力有点大",
    "用户觉得还行吧",
    "用户最近没什么特别的",
    "用户说下次再说",
    "用户有点想不起来",
    "用户只是随口一提",
    "用户今天心情一般",
    "用户不知道该怎么办",
    "用户问了句吃了没",
    "用户随口说了句天气不错",
    "用户说自己还好",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vector", action="store_true", help="跳过向量，只测词法")
    ap.add_argument("--top-k", type=int, default=4)
    args = ap.parse_args()

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="retbench_"))
    mem = Memory(str(tmp / "bench.db"))
    use_vector = (not args.no_vector) and embed_health()

    for _cat, content, _q, _k in CASES:
        mem.upsert_semantic(kind="fact", content=content, confidence=0.7,
                            importance=0.6)
    for content in DISTRACTORS:
        mem.upsert_semantic(kind="fact", content=content, confidence=0.7,
                            importance=0.6)

    index_obj = VectorIndex(tmp / "index.json")
    if use_vector:
        pairs = [(f"s{item['id']}", item["content"]) for item in mem.all_semantic()]
        index_obj.add_texts(pairs)

    rules = decay_engine.load_rules()
    print(f"埋点 {len(CASES)} 条 + 干扰 {len(DISTRACTORS)} 条"
          f"　向量：{'开' if use_vector else '关'}　top{args.top_k}")
    print("=" * 88)
    print(f"{'类别':<10}{'问题':<24}{'目标':<10}{'排名':>5}  命中")
    print("-" * 88)

    hit = 0
    rr = 0.0
    top1_right = 0
    top1_noise = 0
    by_cat: dict[str, list[int]] = {}
    misses: list[str] = []
    distractor_set = set(DISTRACTORS)
    for cat, _content, query, expect in CASES:
        result = R.retrieve(query, mem, index=index_obj,
                            top_k_semantic=args.top_k, rules=rules,
                            record_access=False)
        got = [f"{it['content']}" for it in (result.get("semantic") or [])]
        rank = next((i + 1 for i, g in enumerate(got) if expect in g), 0)
        by_cat.setdefault(cat, []).append(rank)
        hit += bool(rank)
        rr += (1.0 / rank) if rank else 0.0
        # ⚠ "排第 1 的是什么"要单独看：泛泛的**干扰句**在嵌入空间里靠近"中心"，
        #   跟谁的相似度都不低，于是常抢到第 1（实测 `用户最近没什么特别的`
        #   抢过好几道题的榜）。只报 hit@4 会把这个毛病完全盖住。
        first = got[0] if got else ""
        if first and expect in first:
            top1_right += 1
        elif first in distractor_set:
            top1_noise += 1
        if not rank:
            misses.append(f"{cat}：{query}（目标 {expect}）")
        print(f"{cat:<10}{query:<24}{expect:<10}{rank if rank else '-':>5}  "
              f"{'✅' if rank else '✗'}  {(got[0] if got else '（空）')[:30]}")

    print("=" * 88)
    print(f"  hit@{args.top_k}：{hit}/{len(CASES)}　"
          f"MRR：{rr/len(CASES):.3f}　"
          f"**第1就是对的**：{top1_right}/{len(CASES)}　"
          f"第1是干扰句：{top1_noise}/{len(CASES)}")
    print()
    print("  分类小计（**诊断用**——不同类别该由不同机制赢）：")
    print(f"  {'类别':<12}{'hit':>8}{'MRR':>9}   说明")
    NOTES = {
        "口头改写": "问法与原文用词不同 → **向量该赢**",
        "字面重合": "原文就有这些词 → **词法该赢**",
        "否定例外": "否定词是关键",
        "多主体": "**不许串**（串了就是把别人的事记成用户的）",
        "状态变更": "问\"以前\"能不能拿到旧的",
        "数字时间": "数字一字不差，不该被模糊掉",
        "长尾专名": "**IDF 该起作用**",
    }
    for cat, ranks in by_cat.items():
        c_hit = sum(1 for r in ranks if r)
        c_rr = sum((1.0 / r) if r else 0.0 for r in ranks) / len(ranks)
        print(f"  {cat:<12}{c_hit:>4}/{len(ranks):<3}{c_rr:>9.3f}   {NOTES.get(cat,'')}")
    if misses:
        print()
        print("  没检索到的：")
        for m in misses:
            print(f"    · {m}")
    mem.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
