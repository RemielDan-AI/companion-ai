"""B1 查库：记忆变体的埋点事实有没有被提取、能不能被召回（只读诊断）。

做法：隔离库 + 走 app 同一套链路喂进埋点句，然后看
1) 语义记忆里到底存了什么；
2) 问"你还记得…"时检索能不能把它们带回来；
3) 最终注入模型看的记忆块长什么样。

用法：python tests/memory_probe.py [方向] [变体]
"""
from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "dialogues"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import Companion  # noqa: E402
from engine.retrieve import build_memory_block, retrieve  # noqa: E402
import framework  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "probe"
QUERIES = [
    ("你还记得我叫什么吗", ["林一"]),
    ("我对什么过敏来着", ["芒果"]),
    ("我在学什么乐器", ["吉他"]),
    ("我最喜欢哪个季节", ["秋"]),
    ("我住在哪", ["成都", "杭州"]),
]


def main() -> int:
    direction = sys.argv[1] if len(sys.argv) > 1 else "记忆连贯"
    variant = sys.argv[2] if len(sys.argv) > 2 else "搬家换城市"
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    companion = Companion(memory_path=TMP / "db", index_path=TMP / "index.json",
                          profile_path=TMP / "p.json", values_path=TMP / "v.yaml",
                          scenarios_path=TMP / "s.jsonl",
                          preferences_path=TMP / "pr.jsonl",
                          ai_profile_path=TMP / "a.yaml")
    print(f"嵌入服务在用吗：{companion.index is not None}（False＝检索退化成关键词）")

    turns = framework.build(direction, variant)
    seed = [t for t in turns[:20] if isinstance(t, str)]
    print(f"\n先喂 {len(seed)} 轮埋点（真跑，会调模型）…")
    for index, text in enumerate(seed, 1):
        try:
            companion.say(text)
        except Exception as exc:
            print(f"  {index}. 出错：{exc}")
        if index % 5 == 0:
            print(f"  …已 {index} 轮")

    print("\n=== 语义记忆里存了什么 ===")
    rows = companion.memory.all_semantic()
    for item in rows:
        print(f"  [{item['kind']}] {item['content']}")

    print("\n=== 检索能不能召回 ===")
    for query, expects in QUERIES:
        result = retrieve(query, companion.memory, companion.index,
                          exclude_ids=set(), record_access=False)
        got = [(item.get("kind"), str(item.get("content"))[:26], item.get("score"))
               for item in (result.get("semantic") or [])[:4]]
        hit = any(word in " ".join(str(x[1]) for x in got) for word in expects)
        print(f"\n问：{query}　期望命中：{'/'.join(expects)}　→ {'✓' if hit else '✗'}")
        for kind, content, score in got:
            print(f"     {kind} {score}　{content}")
        block = build_memory_block(result)
        print("     注入块：" + (block.replace("\n", " ")[:150] if block else "（空）"))

    companion.memory.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
