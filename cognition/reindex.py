"""索引回填：把「库里有、索引里没有」的记忆补上向量。

## 为什么需要

`cognition/extract.py:297` 把 `index.add_texts()` 的异常**全吞掉**了
（`except Exception: pass`）——这个取舍是对的：嵌入服务挂了不该影响对话。
但**全仓没有回填机制**，于是：

    · 嵌入服务关闭期间写入的记忆，**永远不会被向量化**
    · 改了 embedding 模型/维度，旧向量与新的不匹配，也不会重建
    · 索引文件被删/损坏，没有任何自愈

实测证据（2026-10-15）：`cognition/memory.db` 里情景 7 条 + 语义 1 条，
而 `cognition/vectors/embeddings.json` 只有 3 条（键 `e1,e2,s1`）
→ **`e3`–`e7` 没索引**。

## 取向

和自动拉起嵌入服务一样：**永不抛异常**，只返回状态字典；
嵌入服务不可用时**立刻返回**，不逐条重试（那会把启动拖死）。

统计口径：`added / missing / failed / remaining`。
`limit` 用来限制单次工作量（启动时别把窗口卡住），
按需（CLI 或 `--all`）时可以不限。

用法：
    python -m cognition.reindex                 # 按需全量回填
    python -m cognition.reindex --dry-run       # 只看缺多少
    python -m cognition.reindex --limit 50      # 只补 50 条
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

from cognition.memory import Memory
from cognition.vectors import EmbeddingUnavailable, VectorIndex
from cognition.vectors import health as embed_health

# 单批最多向量化多少条（一次 embed 请求太大容易超时；也便于打印进度）。
# 注意这是**单批**上限，不是总量上限——总量由 limit 控制。
BATCH = 32


def pending_items(memory: Memory, index: VectorIndex,
                  include_cold: bool = True) -> list[tuple[str, str]]:
    """找出「库里有、索引里没有」的条目，返回 [(索引键, 文本)]。

    键的约定与检索一致（`engine/retrieve.py`）：
        语义记忆 -> `s{id}`，索引用 content
        情景记忆 -> `e{id}`，索引用 summary+user_text+reply_text
    （情景那三段拼法与 `retrieve.py` 的关键词兜底保持一致，别只索引 summary，
      否则"我妹妹叫什么"这种问法匹配不到正文里提到的名字。）
    """
    have = index.keys()
    out: list[tuple[str, str]] = []
    for item in memory.all_semantic():
        if not include_cold and int(item.get("archived") or 0):
            continue
        key = f"s{item['id']}"
        if key in have:
            continue
        text = str(item.get("content") or "").strip()
        if text:
            out.append((key, text))
    for item in memory.recent_episodes(limit=100000):
        if not include_cold and int(item.get("archived") or 0):
            continue
        key = f"e{item['id']}"
        if key in have:
            continue
        text = " ".join(str(item.get(f) or "") for f in
                        ("summary", "user_text", "reply_text")).strip()
        if text:
            out.append((key, text))
    return out


def backfill_index(memory: Memory | None = None, index: VectorIndex | None = None,
                   limit: int | None = None, dry_run: bool = False,
                   probe: bool = True, verbose: bool = False) -> dict:
    """把缺失的向量补上。**永不抛异常。**

    返回 {ok, action, added, missing, failed, remaining, detail}
    action ∈ done / dry_run / nothing / no_embed / partial / failed
    """
    try:
        memory = memory if memory is not None else Memory()
        index = index if index is not None else VectorIndex()
    except Exception as exc:
        return {"ok": False, "action": "failed", "detail":
                f"{type(exc).__name__}: {exc}", "added": 0, "missing": 0,
                "failed": 0, "remaining": 0}

    try:
        todo = pending_items(memory, index)
    except Exception as exc:
        return {"ok": False, "action": "failed", "detail":
                f"扫描失败 {type(exc).__name__}: {exc}", "added": 0,
                "missing": 0, "failed": 0, "remaining": 0}

    missing = len(todo)
    if not missing:
        return {"ok": True, "action": "nothing", "added": 0, "missing": 0,
                "failed": 0, "remaining": 0, "detail": "索引已是最新"}

    if dry_run:
        return {"ok": True, "action": "dry_run", "added": 0, "missing": missing,
                "failed": 0, "remaining": missing,
                "detail": f"缺 {missing} 条（未写入）"}

    # 嵌入服务不可用就**立刻返回**：逐条试会把启动拖死，而且每条都会超时。
    if probe and not embed_health():
        return {"ok": False, "action": "no_embed", "added": 0, "missing": missing,
                "failed": 0, "remaining": missing,
                "detail": "嵌入服务不可用，跳过回填（检索会退化为纯词法）"}

    target = todo if limit is None else todo[:max(0, int(limit))]
    added = failed = 0
    for i in range(0, len(target), BATCH):
        chunk = target[i:i + BATCH]
        try:
            added += index.add_texts(chunk)
        except EmbeddingUnavailable as exc:
            # 服务中途挂了：停手，把剩下的如实报出来，别装作补完了
            failed += len(chunk)
            rest = len(target) - i - len(chunk) + (missing - len(target))
            return {"ok": False, "action": "partial", "added": added,
                    "missing": missing, "failed": failed, "remaining": rest,
                    "detail": f"嵌入服务中途不可用：{exc}"}
        except Exception as exc:
            failed += len(chunk)
            if verbose:
                print(f"  这批失败（继续）：{type(exc).__name__}: {exc}")
    remaining = missing - added - failed + max(0, missing - len(target))
    ok = failed == 0
    return {"ok": ok, "action": "done" if ok else "partial", "added": added,
            "missing": missing, "failed": failed, "remaining": max(0, remaining),
            "detail": f"补了 {added}/{missing} 条"
                      + (f"，失败 {failed}" if failed else "")}


def spawn_background(memory: Memory, index: VectorIndex,
                     limit: int | None = 300, verbose: bool = True) -> None:
    """启动时用：丢到**后台线程**补，绝不拖慢窗口。

    为什么限 `limit`：首次跑到一个积压很多的库时，全量补可能要几十秒；
    启动路径上宁可分几次补完，也不要让界面等。
    """
    import threading

    def _run() -> None:
        # ① 别名回填（2026-10-16）：处理「**简称先到、全名后到**」——
        #    用户先说「小雨是广州大学的学生」（那时还不知道全名「林小雨」），
        #    入库那一刻学不到别名，得回头拿现在的全名去历史原话里找证据。
        #    纯本地字符串匹配，不依赖任何服务。
        #    放这里的理由：与索引回填是同一类活（**把派生状态补成与库一致**），
        #    而这个线程已经接到三个入口上了，不必再多接一遍。
        try:
            from cognition.alias import backfill as backfill_alias
            got = backfill_alias(memory)
            if verbose and got.get("added"):
                names = "、".join(f"{a['alias']}→{a['canonical']}"
                                 for a in (got.get("aliases") or [])[:3])
                print(f"[记忆别名] 补学 {got['added']} 条：{names}", flush=True)
        except Exception:
            pass                      # 别名是增强，失败绝不能影响启动
        # ② 向量索引回填
        try:
            res = backfill_index(memory, index, limit=limit, verbose=False)
        except Exception:
            return
        if verbose and res.get("action") in ("done", "partial") and res.get("added"):
            extra = (f"；还有 {res['remaining']} 条待补"
                     if res.get("remaining") else "")
            print(f"[记忆索引] 回填 {res['added']} 条{extra}", flush=True)

    t = threading.Thread(target=_run, name="index-backfill", daemon=True)
    t.start()


def main() -> int:
    ap = argparse.ArgumentParser(description="把缺失的记忆向量补上")
    ap.add_argument("--limit", type=int, default=0, help="最多补几条（0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只看缺多少，不写")
    ap.add_argument("--no-cold", action="store_true", help="跳过冷存储记忆")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    mem = Memory()
    idx = VectorIndex()
    print(f"库：{mem.path}")
    print(f"索引：{idx.path}（现有 {len(idx)} 条）")
    print(f"嵌入服务：{'可用' if embed_health() else '不可用'}")
    t0 = time.time()
    res = backfill_index(mem, idx, limit=(args.limit or None),
                         dry_run=args.dry_run)
    print()
    print(f"结果：{res['action']}　{res['detail']}　（{time.time() - t0:.1f}s）")
    print(f"  缺 {res['missing']}　补 {res['added']}　失败 {res['failed']}"
          f"　剩余 {res['remaining']}")
    print(f"  索引现在 {len(idx)} 条")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
