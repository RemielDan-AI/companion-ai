"""钉住「冲突合并」：模型合并 + 硬护栏 + 失败什么都不改。

## 为什么要这么硬的护栏

合并是**破坏性**操作——它改写长期记忆。模型（尤其 2B）会编，
而**记忆写错比句子丑严重得多**（用户不会知道它错了）。
所以取向是**宁可不合并，也不写错**：

    护栏：数字必须来自原文 / 实词用字必须来自原文 / 长度上界 / 主体必须在
    失败：保持未决，**两条都留着、都可检索**，绝不自动删旧条

实测真 2B（`tests/analysis/merge_probe.py`）：6 例合成 5 例、要点未丢 5 例，
编造（北京／张伟／苏州／凭空数字）**全部被拦**。

本测试**不连模型、不起服务**。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.merge import (apply_merge, check_merge,  # noqa: E402
                             merge_pair)
from cognition.memory import Memory                    # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


class Stub:
    def __init__(self, text: str = "", boom: bool = False):
        self.text, self.boom = text, boom

    def complete(self, *a, **k) -> str:
        if self.boom:
            raise RuntimeError("服务不可用")
        return self.text


def main() -> int:
    old, new = "对方住在南京", "对方搬到成都"

    print("=" * 78)
    print("① 护栏：正常合并要放行（虚词/连接词不算编造）")
    print("=" * 78)
    for text, want, why in (
        ("对方从南京搬到成都", True, "带连接词的正常合并"),
        ("对方现在住在成都", True, "推翻式简写"),
        ("对方先后住在南京并搬到成都", True, "把变化过程写出来"),
        ("对方住在南京或成都", True, "两个都写进去"),
    ):
        ok, reason = check_merge(text, old, new)
        check(ok == want, why, f"{text!r} → {reason or '通过'}")

    print()
    print("=" * 78)
    print("② 护栏：编造必须拦下（这是它存在的理由）")
    print("=" * 78)
    for text, why, hint in (
        ("对方搬到北京", "生造地点", "北/京不在原文"),
        ("对方改名叫张伟", "生造人名", "张/伟不在原文"),
        ("对方在3月搬到成都", "凭空数字", "3 不在原文"),
        ("搬到成都了", "丢了主体「对方」", ""),
        ("", "空", ""),
    ):
        ok, reason = check_merge(text, old, new)
        check(not ok, why, f"{text!r} → {reason or '⚠ 竟然通过了'}")

    print()
    print("=" * 78)
    print("③ merge_pair：模型死了 → unresolved，**不改任何东西**")
    print("=" * 78)
    got = merge_pair(Stub(boom=True), old, new)
    check(got["action"] == "unresolved", "模型不可用 → unresolved", got["reason"])
    check(merge_pair(None, old, new)["action"] == "unresolved", "没有模型 → unresolved")
    got_bad = merge_pair(Stub("对方搬到北京"), old, new)
    check(got_bad["action"] == "unresolved", "编造被护栏拒 → unresolved",
          got_bad["reason"])

    print()
    print("=" * 78)
    print("④ 落库：合并后新条=合成句、旧条 superseded 但**没删**")
    print("=" * 78)
    d = pathlib.Path(tempfile.mkdtemp())
    m = Memory(str(d / "m.db"))
    oid, _ = m.upsert_semantic("fact", old, confidence=0.7)
    nid, _ = m.upsert_semantic("fact", new, confidence=0.7)
    before = sorted(i["content"] for i in m.all_semantic(status="active"))
    check(before == sorted([new, old]), "冲突后两条都 active（不隐身）", str(before))
    apply_merge(m, nid, oid, "对方从南京搬到成都")
    act = [i["content"] for i in m.all_semantic(status="active")]
    check(act == ["对方从南京搬到成都"], "合并后只剩合成句在 active", str(act))
    check(m.entry("semantic", oid)["status"] == "superseded", "旧条标 superseded")
    check(m.entry("semantic", oid)["content"] == old, "旧条内容**原样保留**（可追溯）")
    check(int(m.entry("semantic", oid)["superseded_by"]) == nid, "旧条指向新条")
    check(int(m.entry("semantic", nid)["conflict_with"]) == 0, "冲突标记已清")
    check(not m.pending_conflicts(), "不再列为未决")
    check(any(i["action"] == "merge" for i in m.recent_logs(10)), "合并进审计日志")

    print()
    print("=" * 78)
    print("⑤ 历史遗留：以前被 pending 藏起来的条目，重开库时要救回来")
    print("=" * 78)
    d2 = pathlib.Path(tempfile.mkdtemp())
    p2 = d2 / "m.db"
    m2 = Memory(str(p2))
    m2.upsert_semantic("fact", old, confidence=0.7)
    n2, _ = m2.upsert_semantic("fact", new, confidence=0.7)
    import sqlite3
    with sqlite3.connect(str(p2)) as c:
        c.execute("UPDATE semantic SET status='pending' WHERE id=?", (n2,))
        c.commit()
    check(len(m2.all_semantic(status="active")) == 1, "塞回 pending 后确实被藏")
    m3 = Memory(str(p2))          # 重开触发迁移
    check(len(m3.all_semantic(status="active")) == 2,
          "重开后救回来了（迁移只翻 conflict_with>0 的）", 
          str([i["content"] for i in m3.all_semantic(status="active")]))

    print()
    print("=" * 78)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
