"""量一件事：**向量相似度能不能当别名裁决的辅助信号**？

## 结论：**不能。而且换了更强的嵌入模型也不行**（2026-10-17 实测）

试了 4 个嵌入模型（`ALIAS_VECTOR_MODEL` 切；bge-base 的 **f16 那份 GGUF 是坏的**，
见下），**每一个在 100% 精确率下召回都只有 40%**：

    模型                    该连均值  不该连均值   关键反转                     100%精确下召回
    bge-small-zh-v1.5 f16    0.849    0.610     王小雨0.863 > 小雨/林小雨0.837     40%
    bge-base-zh-v1.5  q8_0   0.818    0.550     **大雨0.708 > 小雨/林小雨0.703**  40%
    bge-base-zh-v1.5  q4_k_m 0.818    0.553     —                                40%
    Qwen3-Embedding-0.6B f16 0.803    0.633     **王小雨0.822 > 小雨/林小雨0.652** 40%

**最干净的一个例子**（bge-base）：
    `小雨 / 林小雨`（**该连**）  = 0.703
    `小雨 / 大雨`  （**不该连**）= 0.708　← **比该连的还高**
把「小雨」连到「大雨」（雨 vs 大雨）比连到「林小雨」还像。

## 根因：**它量的是"像不像"，不是"是不是同一个"**

「林小雨」和「王小雨」在语义上**确实像**——都是三字中文人名、都以「小雨」结尾；
而「小雨」和「林小雨」是**同一个实体**，但字符串差异被模型当成**不同的名字**。
**任务要的是"同一性"，工具给的是"相似性"，两者不是一回事。**
换更大的模型只会在"昵称"和"形似名"之间**换一个失败点**
（Qwen3-Embedding 把 `阿雨/林小雨` 从 0.654 提到 0.693，
但同时把 `林小雨/王小雨` 从 0.792 推到 **0.822**）。

## 为什么现有规则反而安全

它要求**子串同一性**（简称必须是正名的后缀/前缀），
对「小雨/大雨」「林小雨/王小雨」这种**形似但不同**的**天生免疫**。

代价还不对称：漏连只是没认出是同一个人（**不影响正确性**）；
**误连会把两个人的记忆并到一起，而且不可逆**。
现有规则链实测误认 0（`alias_model_probe.py`）→ **加向量是净损失**。

## ⚠ 踩过的坑（都留在这里，避免重犯）

    ① 第一版拿 `compatible(rel_type(a), rel_type(b))` 当"规则裁决" ——
       那只是**类型相符**一个子检查（空类型恒为真），全显示"可连"，**结论是错的**。
    ② 第二版改调 `add_alias()`，但在**空库**上调，绕过了**出处校验和类型信息**
       （生产路径会传 `kind`）→ 空库上 `团子→林小雨` 竟被"接受"，**仍然不忠实**。
       最终只验规则**赖以安全的那条前提**：子串同一性。
    ③ `bge-base-zh-v1.5` 的 **f16 GGUF 是坏的**：
       `E gguf_init_from_reader: duplicate key 'tokenizer.ggml.bos_token_id'`
       ——新版 llama.cpp **严格拒绝**重复元数据键（PR #24916 / issue #24885）。
       同仓库的 **q8_0 / q4_k_m 正常**，用那两个。

用法：
    python tests/analysis/alias_vector_run.py
    $env:ALIAS_VECTOR_MODEL='*base*.gguf'   # 换模型（默认 *bge*.gguf）
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# (简称, 正名, 该不该连, 说明)
PAIRS = [
    # ---- 该连 ----
    ("小雨", "林小雨", True, "后缀简称（规则能抓）"),
    ("阿雨", "林小雨", True, "**昵称，没有子串关系**（规则抓不到）"),
    ("小团", "团子", True, "**宠物昵称，无子串关系**"),
    ("团子", "团子", True, "完全相同"),
    ("广州大学", "广州大学", True, "完全相同"),
    # ---- 不该连（**最关键的一组**）----
    ("小雨", "大雨", False, "**同形状的雨字名**"),
    ("小雨", "陈雨", False, "**都是两字雨名**"),
    ("林小雨", "王小雨", False, "**共享「小雨」**"),
    ("林小雨", "林小丽", False, "**共享「林小」**"),
    ("林小雨", "陈思远", False, "都是三字人名"),
    ("团子", "林小雨", False, "宠物 vs 人"),
    ("妹妹", "林小雨", False, "关系词 vs 名字"),
    ("广州大学", "广州", False, "机构 vs 地名（包含关系但不同实体）"),
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
    for a, b, _w, _why in PAIRS:
        for n in (a, b):
            if n not in names:
                names.append(n)
    vectors = embed(names)
    vec = dict(zip(names, vectors))

    print("别名向量探针（嵌入：bge-small-zh-v1.5）")
    print("=" * 96)
    print(f"{'简称':<10}{'正名':<12}{'该连':<6}{'余弦':>8}   说明")
    print("-" * 96)
    rows = []
    for a, b, want, why in PAIRS:
        s = _cos(vec[a], vec[b])
        rows.append((a, b, want, s))
        print(f"{a:<10}{b:<12}{'✅ 是' if want else '✗ 否':<6}{s:>8.3f}   {why}")

    yes = [s for _a, _b, w, s in rows if w]
    no = [s for _a, _b, w, s in rows if not w]
    print()
    print("=" * 96)
    print(f"  该连的相似度： 最小 {min(yes):.3f}　最大 {max(yes):.3f}　"
          f"均值 {sum(yes)/len(yes):.3f}")
    print(f"  不该连的相似度：最小 {min(no):.3f}　最大 {max(no):.3f}　"
          f"均值 {sum(no)/len(no):.3f}")

    # 最优阈值（扫描），**精确率优先**
    best = None
    for i in range(1, 200):
        t = i / 200
        tp = sum(1 for _a, _b, w, s in rows if w and s >= t)
        fp = sum(1 for _a, _b, w, s in rows if not w and s >= t)
        fn = sum(1 for _a, _b, w, s in rows if w and s < t)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        if best is None or (prec, rec) > (best[1], best[2]):
            best = (t, prec, rec, fp)
    t, prec, rec, fp = best
    print()
    print(f"  最优阈值 {t:.3f} → 精确率 {prec:.0%}　召回率 {rec:.0%}　"
          f"误连 {fp} 对")
    if fp:
        print(f"    ⚠ 误连的是："
              + "、".join(f"{a}/{b}" for a, b, w, s in rows if not w and s >= t))
    if prec < 1.0:
        print("    ⚠ **只要精确率不是 100%，就有误连风险**——"
              "而误连会把两个人的记忆并在一起、且不可逆。")
    print()
    print("=" * 96)
    print("规则为什么安全：它要求**子串同一性**（简称必须是正名的后缀/前缀）")
    print("=" * 96)
    # ⚠ 不要在这里调 `add_alias()` 来"测规则"——那需要**真实的库上下文**
    #   （出处：候选必须独立出现在原话里；类型：`kind` 来自已存的关系）。
    #   在空库上调会绕过这两样，得到**误导性结论**
    #   （实测：空库上 `团子→林小雨` 竟被"接受"，因为两边类型都为空、恒相符）。
    #   这里只验规则**赖以安全的那一条前提**：子串同一性。
    def substring_rule(a: str, b: str) -> bool:
        """生产的后缀/前缀简称规则：简称必须是正名的连续子串，且更短。"""
        return len(a) < len(b) and (b.endswith(a) or b.startswith(a))

    rule_right = vec_right = 0
    for a, b, want, s in rows:
        got = substring_rule(a, b)
        rule_right += (got == want)
        vec_got = s >= t
        vec_right += (vec_got == want)
        print(f"  {'✅' if got == want else '·'} {a}/{b:<10} 该连={str(want):<5}"
              f"　子串规则={'可连' if got else '不连'}"
              f"　向量 {s:.3f} → {'可连' if vec_got else '不连'}")
    print()
    print(f"  子串规则判对 {rule_right}/{len(rows)}　向量判对 {vec_right}/{len(rows)}"
          f"（阈值 {t:.3f}）")
    print("  ⚠ 「阿雨/林小雨」「小团/团子」子串规则抓不到（没有子串关系）——")
    print("    那两个是**模型闭集提议**负责的，不是规则负责的，所以这里记为未命中不算规则错。")
    print()
    print("判读（**决定性的一条在最上面**）：")
    print("  · 该连的最低分（0.649）**低于**不该连的最高分（0.863）→")
    print("    **不存在能把两者分开的阈值**。取高分保精确率，就丢掉大半召回。")
    print("  · 规则靠**子串同一性**，对「小雨/大雨」「林小雨/王小雨」这种**形似**天生免疫；")
    print("    向量是**模糊相似**，恰恰在这些对上给高分 → 它偏的方向就是误连。")
    print("  → **不接**。加了是净损失（精确率掉、不可逆的误连风险涨）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
