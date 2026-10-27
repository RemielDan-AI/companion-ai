"""量嵌入模型的**单次调用延迟**与**向量维度**——换模型的两个实际代价。

## 为什么要量

检索路径上，**每轮对话都要把用户那句 embed 一次**（`_score_semantic` 的向量分）。
嵌入服务是 `ngl=0`（**跑 CPU**，显存留给聊天和抽取），所以：

    · 模型越大 → 每轮检索越慢（用户在等的延迟）
    · 维度不同 → **存量向量全部作废，必须全量重建索引**

`retrieval_bench` 只告诉我们"检索准不准"，这两个代价它不量。

用法：
    python tests/analysis/alias_vector_run.py  （EMBED_SCRIPT=embed_latency.py）
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEXTS = [
    "我妹妹对什么过敏来着",
    "用户妹妹林小雨在工业软件公司上班",
    "我方对芒果过敏",
    "我今天很累，什么都不想干",
    "用户睡前看半小时书",
    "团子三岁了",
    "我在哪上班",
    "我妈在杭州住院了",
    "小花是我妹妹的同学",
    "我平时在哪买花生",
]
WARMUP = 2
ROUNDS = 5


def main() -> int:
    from cognition.vectors import embed, health, INDEX_PATH
    if not health():
        print("嵌入服务（8081）不在线")
        return 1

    v0 = embed(["测试"])[0]
    dim = len(v0)
    print(f"向量维度：{dim}")
    print(f"索引文件：{INDEX_PATH}")
    print(f"（⚠ 维度换了 → 存量向量全部作废，必须 `services/reindex.py` 全量重建）")
    print()

    embed(TEXTS[:WARMUP])                       # 预热
    # 单条（＝每轮对话检索的真实开销）
    single = []
    for _ in range(ROUNDS):
        for t in TEXTS:
            t0 = time.time()
            try:
                embed([t])
            except Exception as exc:
                print(f"  失败：{exc}")
                return 1
            single.append(time.time() - t0)
    # 批量（＝重建索引时的开销）
    t0 = time.time()
    embed(TEXTS)
    batch = time.time() - t0

    single.sort()
    n = len(single)
    print(f"单条 embed（每轮检索的真实开销，{n} 次）：")
    print(f"  中位 {single[n//2]*1000:.0f} ms　"
          f"p90 {single[int(n*0.9)]*1000:.0f} ms　"
          f"最快 {single[0]*1000:.0f} ms　最慢 {single[-1]*1000:.0f} ms")
    print(f"整批 {len(TEXTS)} 条：{batch*1000:.0f} ms")
    print()
    print(f"对照：抽取一次约 1000 ms、聊天生成远大于此。")
    print(f"      每轮多出的检索延迟 = 上面那个中位数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
