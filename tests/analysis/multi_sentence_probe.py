"""三句**互补**说法会不会被合并/关联？——真实链路实测。

## 场景（用户提的）

    第 1 轮：我妹妹在广州上学。
    第 2 轮：我妹妹叫林小雨。
    第 3 轮：小雨是广州大学的学生。

这三句是**互补**（不是"同栏位换值"），所以 `conflict_pair` **不应该**判冲突，
也就不会走冲突合并。但真正要问的是三件事，本探针逐个量：

    ① **存成几条**？—— 有没有被合并成一条关于「妹妹/林小雨」的记录
    ② **别名认不认**？—— 「小雨」和「林小雨」是同一个人的简称，
       系统有没有把它们当成一个实体（这是最关键的一条）
    ③ **检索得到吗**？—— 问「我妹妹在哪个学校」能不能把三句拼起来答出来

## 为什么必须实测

前面几轮反复证明：合成数据 / 想当然的推断会骗人。
（例如我一度以为一跳扩展把多跳从 0/3 提到 2/3，其实是手写的假记忆。）

用法（抽取器由本脚本自己拉起）：
    python tests/model_server.py run --model v1.3 --ngl 15 \
        --script tests/analysis/multi_sentence_probe.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TURNS = ["我妹妹在广州上学", "我妹妹叫林小雨", "小雨是广州大学的学生"]

# (问题, 期望答案的任一关键词)
QUESTIONS = [
    ("我妹妹在哪个学校", ["广州大学", "广州"]),
    ("林小雨在哪上学", ["广州大学", "广州"]),
    ("小雨是哪个学校的", ["广州大学", "广州"]),
]


def main() -> int:
    try:
        from services.autostart import ensure_extract_service
        r = ensure_extract_service(verbose=False)
        print(f"抽取器：{r['action']}　{r['detail']}")
        os.environ["EXTRACT_URL"] = "http://127.0.0.1:8083/v1/chat/completions"
    except Exception as exc:
        print(f"抽取器没起来（{exc}）")

    import app as A
    from engine.retrieve import known_names
    d = pathlib.Path(tempfile.mkdtemp())
    c = A.Companion(memory_path=d / "m.db", index_path=d / "i.json",
                    profile_path=d / "p.json", values_path=d / "v.yaml",
                    scenarios_path=d / "sc.jsonl", preferences_path=d / "pf.jsonl",
                    ai_profile_path=d / "ai.json")

    print("=" * 88)
    print("逐轮入库")
    print("=" * 88)
    for i, u in enumerate(TURNS, 1):
        c.say(u)
        print(f"  第{i}轮：{u}")
    print()
    print("语义库里现在有：")
    for it in c.memory.all_semantic(status="active"):
        print(f"  [{it['kind']:>9}] {it['content']}")
    sem = c.memory.all_semantic(status="active")
    print()
    print(f"→ 存成 **{len(sem)} 条**（没有合并：这三句是互补，不是冲突）")

    print()
    print("=" * 88)
    print("② 别名认不认：别名表 / known_names 看到哪些实体")
    print("=" * 88)
    from cognition import alias as AL
    rows = AL.all_aliases(c.memory)
    print(f"  别名表 = {[(r['alias'], r['canonical']) for r in rows]}")
    kn = sorted(known_names(c.memory))
    print(f"  known_names = {kn}")
    for probe in ("林小雨", "小雨", "妹妹"):
        hit = probe in kn
        print(f"  {'✅' if hit else '✗'} 「{probe}」被当成已知实体")
    print()
    if rows:
        print("  ✅ 别名表把「小雨」和「林小雨」连起来了")
    else:
        print("  ✗ 别名表是空的——简称没归并上，换个说法问就召不回来")

    print()
    print("=" * 88)
    print("③ 检索：三句能不能拼起来答出来")
    print("=" * 88)
    for q, wants in QUESTIONS:
        # ⚠ 必须走 `c.say()` 再看 `c.last_used_block`：
        #   直接调 `_select_memory_block` 会拿到**过期的 `self.last_retrieval`**
        #   （它由上层方法先 retrieve 再设），块会是空的。
        reply, _ = c.say(q)
        block = getattr(c, "last_used_block", "") or ""
        ok = any(w in (reply or "") for w in wants)
        print(f"\n  问：{q}")
        print(f"  注入块含答案关键词：{[w for w in wants if w in block]}")
        print(f"  注入块：{(block or '（空）')[:120]}")
        print(f"  答：{(reply or '')[:76]}")
        print(f"  {'✅ 答对' if ok else '✗ 没答出来'}")

    print()
    print("=" * 88)
    print("结论")
    print("=" * 88)
    print(f"  存成 {len(sem)} 条独立记忆（互补事实，按设计不合并）")
    print(f"  别名「小雨」是否被认出：{'是' if '小雨' in kn else '否'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
