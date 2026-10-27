"""**重建的探针**：把"检索/组装"和"模型能不能用"彻底分开，并**把失败分成三层**。

## 为什么要重做（2026-10-18 的结论）

旧探针 `hard_dialogue_probe.py` 是**多轮级联**：每个场景一条连续对话，
前几轮回复不同 → 抽取不同 → 库不同 → 后面全变。
于是 **102 个观测高度相关，不是独立样本**，3 次重复给不出有效样本量。

实测证据：同一天，只往 `persona.yaml` 加一行，总分从 66% 掉到 51%、
计数从 58% 掉到 0%——**机制上解释不了**，只能是级联放大。
那种探针**已经分辨不出 15 个点的差异**。

## 这一版怎么改

    ① **库只建一次、然后冻住**：场景的 `setup` 用真抽取跑一遍建库，
       之后所有采样都用**完全相同的库和块**——唯一变量就是模型的随机性。
    ② **一个问题一次采样**：不再有跨轮级联，观测之间互相独立。
    ③ **把失败分成三层**（这是最有价值的部分）：

        store缺    答案根本不在库里      → **抽取/来源**问题，模型再好也答不出
        block缺    在库里、但没进块       → **检索/组装**问题
        model没用  在块里、模型却没答出   → **模型**问题

       旧探针只能笼统报"失败"，这一版能直接说**该修哪一层**。

## 判分沿用旧探针

`want`（每组可接受说法，全组命中才算过）/ `forbid`（不该出现的说法）
直接复用 `hard_dialogue_probe.py` 的定义，**判据不另起一套**，
否则新旧两版的结果没法比。

用法：
    python tests/model_server.py run --model v1.3 --ngl 15 \
        --script tests/analysis/answer_probe.py
    （`ANSWER_RUNS=5` 控制每个问题的采样次数）
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(HERE))

import os                                                   # noqa: E402

from cognition.assemble import assemble as _assemble        # noqa: E402
from cognition.extract import remember_turn                 # noqa: E402
from cognition.memory import Memory                         # noqa: E402
from engine.generate import Generator                       # noqa: E402
from engine.retrieve import retrieve                        # noqa: E402

# 场景与判据**直接复用旧探针**（判据不另起一套，否则新旧结果没法比）
from hard_dialogue_probe import SCENARIOS, _flat, _score     # noqa: E402

RUNS = max(1, int(os.environ.get("ANSWER_RUNS", "5")))


def build_store(sc: dict) -> tuple[Memory, object]:
    """跑一遍 setup 建库（用真抽取）+ **建向量索引**，之后**冻住**。

    ⚠ **必须建向量索引**（2026-10-18 实测踩坑）：`retrieve()` 不传 `index` 时
      向量检索**整个不启用**，只剩词法 → 「我现在住哪」和库里的「对方住在南京」
      **零字面重合** → 返回 0 条 → 探针把它报成 `block缺`（假的）。
      实测这一条就制造了 2 个假阳性。生产路径是带索引的，这里必须一致。

    ⚠ **抽取必须走 `Extractor`（8083 的 2B），不能把聊天模型直接传进去**
      ——实测踩过：直接传 v1.3 的 Generator 时，8083 起来了但**一个请求都没收到**
      （日志 0.7KB），抽取全由聊天模型代劳 → 库是垃圾 → 报出 4 个假的「store缺」。

    ⚠ 只用一次抽取的样本建库 —— 抽取是随机的，某一场景的库可能恰好缺一条。
      这正是"store缺"那一层要量出来的东西，**不能靠多跑几次把它掩盖掉**。
    """
    from cognition.extractor import Extractor, extract_url
    from cognition.vectors import VectorIndex

    chat = Generator(api_url=os.environ["_PROBE_URL"])
    try:
        ext_gen = Generator(level=None, api_url=extract_url(None))
        gen = Extractor(primary=ext_gen, fallback=chat, verbose=False)
    except Exception:
        gen = chat
    d = pathlib.Path(tempfile.mkdtemp())
    m = Memory(str(d / "probe.db"))
    for i, line in enumerate(sc["setup"], 1):
        try:
            remember_turn(m, gen, line, "嗯", session="probe", turn=i, index=None)
        except Exception:
            pass
    idx = VectorIndex(d / "idx.json")
    try:
        idx.add_texts([(f"s{it['id']}", it["content"])
                       for it in m.all_semantic(status="active")])
    except Exception:
        pass
    return m, idx


def main() -> int:
    from services.autostart import ensure_embed_service, ensure_extract_service
    ensure_extract_service(verbose=False)
    ensure_embed_service(verbose=False)
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    os.environ["_PROBE_URL"] = url
    gen = Generator(api_url=url)
    print(f"重建探针　模型：{os.environ.get('EXTRACT_MODEL_TAG') or url}"
          f"　每问 {RUNS} 次　库建一次即冻结")
    print("=" * 100)

    tally = {"store": 0, "block": 0, "model": 0, "ok": 0}
    by_dim: dict[str, list] = {}
    rows: list[dict] = []

    for sc in SCENARIOS:
        mem, idx = build_store(sc)
        store_blob = " ".join(str(x["content"]) for x in mem.all_semantic(status="active"))
        for p in sc["probes"]:
            q = p["q"]
            want = _flat(p["want"])
            block = ""
            try:
                out = _assemble(q, mem,
                                retrieve(q, mem, index=idx, record_access=False),
                                session="probe", turn=100)
                block = out.get("text") or ""
            except Exception as exc:
                block = f"（组装出错 {exc}）"
            in_store = any(w in store_blob for w in want) if want else True
            in_block = any(w in block for w in want) if want else True
            scores = []
            reasons = []
            for _ in range(RUNS):
                try:
                    reply = gen.chat(q, memory_block=block, remember=False)
                except Exception as exc:
                    reply = f"（生成出错 {exc}）"
                s, miss = _score(reply, p["want"], p["forbid"])
                scores.append(s)
                if miss:
                    reasons.append(miss)
            hit = sum(1 for s in scores if s == 2)
            # ---- 三层归因 ----
            # ⚠ **只对"事实型"探针做库存/块归因**（2026-10-18 实测踩坑）：
            #   行为型探针（能不能吃、会不会告密、吃什么药）的 `want` 是
            #   "模型该说什么"，**不是"库里有什么"**——拿它去查库必然报假 `store缺`，
            #   实测把「命中 5/5 全中」的探针标成了 ✗。
            #
            # ⚠ **判据顺序：先看块、再看库**。要问的是"模型手上有它需要的东西吗"——
            #   在块里就算有。反过来的话，像「我妹妹有几个忌口」这种
            #   **答案是程序算出来的**（"一共 3 条"）会被误判成 `store缺`
            #   （库里当然没有"三"这个字）。
            if p.get("kind") == "behave":
                layer = "ok" if hit == RUNS else "model没用"
            elif in_block:
                layer = "ok" if hit == RUNS else "model没用"
            elif in_store:
                layer = "block缺"
            else:
                layer = "store缺"
            tally[layer] = tally.get(layer, 0) + 1
            by_dim.setdefault(sc["dimension"], []).append((layer, hit, RUNS))
            mark = "✅" if layer == "ok" else ("⚠" if layer == "model没用" else "✗")
            rows.append(dict(dim=sc["dimension"], q=q, layer=layer,
                             hit=hit, runs=RUNS, want=want))
            print(f"{mark} [{sc['dimension']}] {q}")
            print(f"     命中 {hit}/{RUNS}　层={layer}　目标={want}"
                  + (f"　缺：{reasons[0]}" if reasons else ""))

    print()
    print("=" * 100)
    print("**三层归因**（这版探针的主要价值）")
    print("=" * 100)
    total = sum(tally.values())
    print(f"  库和块都到位、模型也用上了   ok        {tally.get('ok',0):>3}/{total}")
    print(f"  **在块里、模型没答出**       model没用 {tally.get('model没用',0):>3}/{total}")
    print(f"  **在库里、没进块**           block缺   {tally.get('block缺',0):>3}/{total}")
    print(f"  **库里根本没有**             store缺   {tally.get('store缺',0):>3}/{total}")
    print()
    print("  分类小计：")
    for dim, items in by_dim.items():
        ok = sum(1 for l, _h, _r in items if l == "ok")
        mm = sum(1 for l, _h, _r in items if l == "model没用")
        bm = sum(1 for l, _h, _r in items if l == "block缺")
        sm = sum(1 for l, _h, _r in items if l == "store缺")
        print(f"    {dim:<12} ok {ok}/{len(items)}　model没用 {mm}　block缺 {bm}　store缺 {sm}")
    print()
    print("读法：")
    print("  · `model没用` 多 → 要改**呈现方式/提示词**（但这块今天连输两次，谨慎）")
    print("  · `block缺` 多   → 改**检索/组装**（能用组装指标确定性验证）")
    print("  · `store缺` 多   → 改**抽取**（能用 extract_metric 验证）")
    print("  · 观测现在**互相独立**（库冻结、一个问题一次采样），所以",
          f"{RUNS} 次就是 {RUNS} 个独立样本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
