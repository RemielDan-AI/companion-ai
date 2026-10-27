"""**辅助审查 2B 的别名提案**：同一个候选词，向量能不能把正确正名排前面？

## 为什么和上一个探针问的不是一回事（2026-10-17 用户纠正）

`alias_vector_probe.py` 问的是"**全局阈值**：这对名字像不像到可以判为同一实体"。
结论是不行（四个模型召回都只有 40%）。

但真实用途**不是**这个。真实链路是：

    ① 2B **提议**：从**已知名字的闭集**里挑一个作为候选词的正名
    ② 程序**审查**：护栏（长度/泛称/歧义/成环/类型）决定收不收

向量要做的是**协助审查**，即在 ② 这一步多一个信号。
那正确的问法是——

    **对同一个候选词，向量能不能把「正确的正名」排在「错误的正名」前面？**

这**不需要全局阈值**，只要**排序**对就行。全局阈值失败（分布重叠）
**不代表排序失败**——这是两件事，我一开始测错了方向。

## 量什么

对每个候选词，给它几个候补正名（一个正确、几个错误），看：
    · 正确正名是否排在**第一**
    · 正确项与错误项的**分差**（分差大 = 这个信号有判别力）
    · 错了的话，错在哪一对（那种就是这条信号**兜不住**的情形）

用法：
    python tests/analysis/alias_vector_rank.py
    $env:ALIAS_VECTOR_MODEL='*base*.gguf'
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ⚠ 这是**真实提案的形态**：2B 从已知名字闭集里挑。
#   候选词（用户原话里的短称/昵称）+ 几个候补正名（**其中只有一个是用户实际指的人**）。
CASES = [
    dict(cand="小雨", right="林小雨",
         wrong=["王小雨", "大雨", "陈雨", "广州大学"],
         why="「小雨」是林小雨的昵称；王小雨/大雨 是**别的实体**"),
    dict(cand="小团", right="团子",
         wrong=["林小雨", "小花", "小园"],
         why="宠物昵称，没有子串关系（规则抓不到，全靠模型提议）"),
    dict(cand="阿雨", right="林小雨",
         wrong=["王小雨", "陈雨", "陈思远"],
         why="昵称的另一形态，同样没有子串关系"),
    dict(cand="妹妹", right="林小雨",
         wrong=["陈思远", "团子", "广州大学"],
         why="**关系词 → 人名**（这条路本来由关联锚点走，看向量帮不帮得上）"),
    dict(cand="公司", right="工业软件公司",
         wrong=["广州大学", "林小雨", "全家便利店"],
         why="机构简称"),
]


def _cos(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / (na * nb) if na and nb else 0.0


def main() -> int:
    from cognition.vectors import embed, health
    if not health():
        print("嵌入服务（8081）不在线——先起它。")
        return 1

    names: list[str] = []
    for c in CASES:
        for n in [c["cand"], c["right"]] + c["wrong"]:
            if n not in names:
                names.append(n)
    vec = dict(zip(names, embed(names)))

    print("别名提案的**辅助审查**：同一候选词，正确正名排第几？")
    print("=" * 96)
    top1 = 0
    for c in CASES:
        scored = [(n, _cos(vec[c["cand"]], vec[n])) for n in [c["right"]] + c["wrong"]]
        scored.sort(key=lambda x: -x[1])
        right_score = dict(scored)[c["right"]]
        rank = [n for n, _s in scored].index(c["right"]) + 1
        best_wrong = max(s for n, s in scored if n != c["right"])
        top1 += (rank == 1)
        print(f"\n候选「{c['cand']}」　{c['why']}")
        for i, (n, s) in enumerate(scored, 1):
            tag = " ← **正确**" if n == c["right"] else ""
            print(f"   {i}. {n:<14}{s:.3f}{tag}")
        if rank == 1:
            print(f"   ✅ 正确项排第一，领先最高的错误项 {(right_score-best_wrong):+.3f}")
        else:
            winner = scored[0][0]
            print(f"   ✗ **正确项只排第 {rank}**，被「{winner}」压过"
                  f"（{scored[0][1]:.3f} vs {right_score:.3f}）")

    print()
    print("=" * 96)
    print(f"  正确项排第一：{top1}/{len(CASES)}")
    print()
    print("判读：")
    print("  · 这是**排序**问题，不是阈值问题——只要正确项稳定排第一，")
    print("    它就能当「提议的复核信号」（例如排序与规则冲突时提请人工/降低置信）。")
    print("  · **不该用它单独拍板**：规则仍然是否决权；向量只做辅助排序。")
    print("  · 排不到第一的那几例，就是这条信号**兜不住**的情形，要如实记下来。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

