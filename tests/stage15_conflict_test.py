#!/usr/bin/env python
"""阶段 15 记忆冲突处理测试（项目改进v1 改进 18）。

验收：新旧矛盾被标记待确认、不自动覆盖而是问用户、旧版本可追溯、冲突日志可查。

用法：python tests/stage15_conflict_test.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition.memory import Memory  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "conflict"


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    memory = Memory(TMP / "c.db")
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 1. 冲突判定：只认"同一件事说了不同的值" ----
    cases = [
        ("对方住在杭州", "对方住在上海", True, "同句换值＝冲突"),
        ("对方喜欢喝美式咖啡", "对方喜欢喝美式咖啡，不加糖", False, "补充信息＝不算冲突"),
        ("对方养了一只猫叫团子", "对方住在杭州", False, "不同话题＝不算冲突"),
        ("对方叫林一", "对方叫林二", True, "名字对不上＝冲突"),
        ("对方住在杭州", "对方住在杭州", False, "完全一样＝不算冲突"),
    ]
    for a, b, want, label in cases:
        got, _detail = Memory.conflict_pair(a, b)
        check(f"判定：{label}", got == want, f"{a} ↔ {b} → {got}")

    # ---- 2. 写入时自动标记冲突，**但不覆盖旧说法、也不让新说法隐身** ----
    old_id, _ = memory.upsert_semantic("fact", "对方住在杭州", confidence=0.7)
    new_id, created = memory.upsert_semantic("fact", "对方住在上海", confidence=0.8)
    check("新说法是新建的（不覆盖旧条目）", created and new_id != old_id,
          f"旧 #{old_id} 新 #{new_id}")
    old_row = memory.entry("semantic", old_id)
    new_row = memory.entry("semantic", new_id)
    check("旧说法仍然原样保留", old_row["content"] == "对方住在杭州"
          and old_row["status"] == "active", f"{old_row['content']}／{old_row['status']}")
    # ⚠ 2026-10-15 改：冲突条**不再写 pending**（pending 被检索挡住，
    #   用户不回答就永久丢失）。现在保持 active，只用 conflict_with 记"取代了谁"。
    check("新说法保持 active 并指向旧条目（不再隐身）",
          new_row["status"] == "active" and int(new_row["conflict_with"]) == old_id,
          f"{new_row['status']} conflict_with={new_row['conflict_with']}")

    # ---- 3. 冲突条目**照常参与检索**（不隐身）；未决清单仍能列出 ----
    active = [item["content"] for item in memory.all_semantic(kind="fact")]
    check("冲突条目照常可检索（不再被 pending 挡掉）",
          "对方住在上海" in active and "对方住在杭州" in active, str(active))
    pending = memory.pending_conflicts()
    check("能列出未决冲突清单",
          len(pending) == 1 and pending[0]["new_id"] == new_id
          and pending[0]["old_id"] == old_id, str(pending))
    check("冲突写进了审计日志",
          any(item["action"] == "conflict" for item in memory.recent_logs(20)), "")

    # ---- 4. 三种拍板方式 ----
    result = memory.resolve_conflict(new_id, "new")
    check("选 new：新说法生效、旧的记 superseded_by",
          memory.entry("semantic", new_id)["status"] == "active"
          and memory.entry("semantic", old_id)["status"] == "superseded"
          and int(memory.entry("semantic", old_id)["superseded_by"]) == new_id,
          f"new={memory.entry('semantic', new_id)['status']}"
          f" old={memory.entry('semantic', old_id)['status']}")
    check("旧的仍在库里可追溯",
          memory.entry("semantic", old_id)["content"] == "对方住在杭州", "")
    check("拍板动作进日志",
          any(item["action"] == "resolve_conflict" for item in memory.recent_logs(10)), "")

    # old 分支
    id_a, _ = memory.upsert_semantic("fact", "对方叫林一", confidence=0.7)
    id_b, _ = memory.upsert_semantic("fact", "对方叫林二", confidence=0.9)
    memory.resolve_conflict(id_b, "old")
    check("选 old：新说法标 rejected、旧说法继续生效",
          memory.entry("semantic", id_b)["status"] == "rejected"
          and memory.entry("semantic", id_a)["status"] == "active", "")

    # both 分支：两只宠物可以同时养，用户说两个都对
    id_c, _ = memory.upsert_semantic("fact", "对方养了一只狗", confidence=0.6)
    id_d, _ = memory.upsert_semantic("fact", "对方养了一只猫", confidence=0.6)
    check("第二只宠物被**判成冲突**（但两条都保持可检索）",
          int(memory.entry("semantic", id_d)["conflict_with"]) == id_c
          and memory.entry("semantic", id_d)["status"] == "active",
          f"{memory.entry('semantic', id_d)['status']}"
          f" conflict_with={memory.entry('semantic', id_d)['conflict_with']}")
    memory.resolve_conflict(id_d, "both")
    both_active = [item["content"] for item in memory.all_semantic(kind="fact")]
    check("选 both：两条都生效",
          "对方养了一只狗" in both_active and "对方养了一只猫" in both_active,
          str(both_active))

    # ---- 5. 非冲突条目不能走冲突流程 ----
    plain, _ = memory.upsert_semantic("fact", "对方喜欢跑步", confidence=0.5)
    try:
        memory.resolve_conflict(plain, "new")
        check("非冲突条目调用拍板会报错", False, "居然成功了")
    except ValueError as exc:
        check("非冲突条目调用拍板会报错", "不是待确认" in str(exc), str(exc)[:40])
    check("处理完的冲突不再出现在待确认里", not memory.pending_conflicts(), "")

    memory.close()
    shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"\n通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
