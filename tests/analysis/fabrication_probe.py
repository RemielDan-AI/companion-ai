"""量「凭空编前情」：明明没有记忆、没有历史，它却自己说「你上次…」「你以前…」。

## 为什么要单独一个探针

三次探针（单句补充 / 合并查漏 / 字数指令）**每次都出现**这个现象：

    上下文为空（history 空、无记忆块）
    → 「你上次也吵了。这次是因为什么？」
    → 「你上次也说这种时候最难的是不让自己动。」

之前 `guard_probe` 的"不编造"是过的，但那些题问的是
「我上次说我妈住哪个医院」——有明确判断依据（要么记得要么不记得）。
**这里是主动添加不存在的前情**，是另一种编造，之前的探针没覆盖。

## 两组对照（必须都有，否则修过头看不出来）

    **A 组（无凭据）**：空上下文 + 中性话题
        → 出现「上次/以前/之前/那次」= **编造**，要压下 0

    **B 组（有凭据）**：给一个含「对方以前住在南京」的记忆块，问「我以前住哪」
        → 出现「以前」= **正确**，不能被压掉
        （⚠ 没有 B 组，就会用"禁词"把人话也一起掐死——这正是
          「不确定的不要说得像确定的」那句话当年踩过的坑。）

用法：
    python tests/model_server.py run --model v1.5 --ngl 15 \
        --script tests/analysis/fabrication_probe.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.assemble import assemble as _assemble   # noqa: E402
from cognition.memory import Memory                    # noqa: E402
from engine.generate import Generator                  # noqa: E402
from engine.retrieve import retrieve                   # noqa: E402

RUNS = max(1, int(os.environ.get("FAB_RUNS", "3")))

# A 组：中性话题，**没有任何前情依据**
NEUTRAL = [
    "今天累死了",
    "刚跟朋友吵了一架",
    "我在想要不要换工作",
    "我最近老是想哭",
    "外面下雨了",
    "周末不知道干嘛好",
    "我有点睡不着",
    "我妈又催我相亲了",
]

# 指涉"过去"的词——出现即怀疑（A 组里出现就是编造）
PAST = ("上次", "上回", "之前", "以前", "那次", "那时候", "前阵子", "当年",
        "上个月", "前几天", "昨天")

# B 组：真有凭据的情形（这些出现「以前」是对的）
LEGIT = [
    ("对方住在南京", "对方已经搬到成都了", "我以前住哪"),
]


def hit_past(text: str) -> list[str]:
    t = str(text or "")
    return [w for w in PAST if w in t]


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"凭空编前情检测　A 组 {len(NEUTRAL)} 题 × {RUNS} 次　"
          f"模型 {os.environ.get('EXTRACT_MODEL_TAG', url)}")
    print("=" * 92)

    # ⚠ **必须走 Companion.say()**：无来源断言守卫在 `say()` 里，
    #   直接调 `gen.chat` 会**绕过守卫**——测出来的只代表"裸模型"，
    #   修没修守卫都看不出来。
    from app import Companion                      # noqa: E402
    comp = Companion(api_url=url, use_memory=False, session="fab",
                     no_extract_service=True)

    bad = 0
    total = 0
    samples = []
    for q in NEUTRAL:
        for _ in range(RUNS):
            comp.generator.history = []      # 确保上下文真的是空的
            try:
                r = str(comp.say(q) or "").strip()
            except Exception as exc:
                r = f"（出错 {exc}）"
            total += 1
            hits = hit_past(r)
            if hits:
                bad += 1
                if len(samples) < 6:
                    samples.append((q, hits, r[:80]))
    print(f"**A 组（无凭据）编造 {bad}/{total}（{100*bad/max(1,total):.0f}%）**"
          f"　← 目标是 0")
    for q, hits, r in samples:
        print(f"    {q} → 命中 {hits}")
        print(f"       {r}")
    print()

    # B 组：有凭据，必须**不**被压掉
    d = pathlib.Path(os.environ.get("TEMP", ".")) / "fab_probe"
    d.mkdir(parents=True, exist_ok=True)
    mem = Memory(str(d / "m.db"))
    for i, (a, b, _q) in enumerate(LEGIT, 1):
        ep = mem.add_episode(user_text=a.replace("对方", "我"), reply_text="嗯",
                             summary=a[:10], session="fab", turn=i)
        mem.upsert_semantic(kind="fact", content=a, confidence=0.8, episodic_id=ep)
        ep2 = mem.add_episode(user_text=b.replace("对方", "我"), reply_text="嗯",
                              summary=b[:10], session="fab", turn=i + 10)
        mem.upsert_semantic(kind="fact", content=b, confidence=0.8, episodic_id=ep2)
    ok = 0
    tot2 = 0
    for a, b, q in LEGIT:
        block = ""
        try:
            block = _assemble(q, mem, retrieve(q, mem, record_access=False),
                              session="fab", turn=100).get("text") or ""
        except Exception:
            block = ""
        for _ in range(RUNS):
            gen.history = []
            try:
                r = str(gen.chat(q, memory_block=block, remember=False) or "").strip()
            except Exception:
                r = ""
            tot2 += 1
            if "以前" in r or "南京" in r:
                ok += 1
    print(f"B 组（有凭据·该说以前）正确 {ok}/{tot2}　← 不能被压掉，目标是 {tot2}")
    print()
    print("判读：")
    print("  · A 组越低越好（0 是目标）；B 组必须保持高，否则是『用禁词掐死人话』")
    print("  · 修完再看这两组，**只有 A 降、B 不降**才算真修好")
    try:
        mem.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
