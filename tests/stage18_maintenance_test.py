#!/usr/bin/env python
"""阶段 18 长期维护测试（项目改进v1 改进 28）。

验收：数据库定期压缩、检索质量定期评估、衰退机制定期检查、备份可用。
全程用临时库与临时备份目录，不碰真实数据。

用法：python tests/stage18_maintenance_test.py
"""
from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition import maintenance  # noqa: E402
from cognition.memory import Memory  # noqa: E402
from cognition.vectors import VectorIndex  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "maint"


def days_ago(n: float) -> str:
    return (dt.datetime.now() - dt.timedelta(days=n)).isoformat(timespec="seconds")


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    memory = Memory(TMP / "m.db")
    index = VectorIndex(TMP / "index.json")
    backups = TMP / "backups"
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # 造点数据：新记忆 + 一条很旧的低重要度记忆 + 一对相似记忆
    memory.add_episode("我养了只猫叫团子", "记住了", summary="养猫叫团子", session="s",
                       turn=1, importance=4)
    memory.add_episode("随便聊了句天气", "嗯", summary="闲聊天气", session="s", turn=2,
                       importance=1)
    memory.upsert_semantic("fact", "对方住在杭州", confidence=0.8)
    memory.upsert_semantic("preference", "对方喜欢喝美式咖啡", confidence=0.6)
    memory.upsert_semantic("preference", "对方喜欢喝美式咖啡，不加糖", confidence=0.5)
    memory.conn.execute(
        "UPDATE episodic SET created_at=?, last_access_at='', peak=1.0, importance=1.0"
        " WHERE summary='闲聊天气'", (days_ago(500),))
    memory.conn.execute(
        "UPDATE semantic SET created_at=?, last_access_at='' WHERE content LIKE '%杭州%'",
        (days_ago(600),))
    memory.conn.commit()

    # ---- 1. 只体检不动数据 ----
    before = (memory.episode_count(), memory.semantic_count("active"))
    result = maintenance.run(memory, index, backup_dir=backups, check_only=True)
    check("体检模式不改数据",
          (memory.episode_count(), memory.semantic_count("active")) == before
          and "quality" in result and "decay" in result, str(before))
    check("体检模式不生成备份", not backups.exists() or not list(backups.glob('*.json')), "")

    # ---- 2. 衰退体检 ----
    health = maintenance.decay_health(memory)
    check("衰退体检给出总量与分布",
          health["total"] == 5 and sum(health["buckets"].values()) == 5, str(health["buckets"]))
    check("能算出平均有效重要度", health["avg_effective"] > 0, str(health["avg_effective"]))
    check("体检结论有人话说明", bool(health["warnings"]), "；".join(health["warnings"]))
    # 把所有记忆一律改旧，再配一个极短半衰期：应当报"贴地板"警告
    memory.conn.execute("UPDATE episodic SET created_at=?, last_access_at=''",
                        (days_ago(900),))
    memory.conn.execute("UPDATE semantic SET created_at=?, updated_at=?, last_access_at=''",
                        (days_ago(900), days_ago(900)))
    memory.conn.commit()
    tight = maintenance.decay_health(memory, {"floor": 0.05, "enabled": True,
                                              "half_life_days": {"default": 0.5},
                                              "retention_factor": {"normal": 1.0}})
    check("半衰期设得太短时给出警告",
          any("半衰期" in w for w in tight["warnings"]), "；".join(tight["warnings"]))

    # ---- 3. 检索质量评估 ----
    quality = maintenance.quality(memory, index, sample=5)
    check("检索质量能评估出命中率",
          quality["checked"] >= 3 and 0 <= quality["rate"] <= 1,
          f"{quality['hit']}/{quality['checked']} = {quality['rate']}")
    check("评估会记录检索模式", isinstance(quality["modes"], dict) and quality["modes"],
          str(quality["modes"]))
    check("命中的能找回来（自召回不为 0）", quality["hit"] >= 1,
          f"漏掉 {quality['misses']}")

    # ---- 4. 整理：转冷 + 合并 + VACUUM ----
    report = maintenance.run(memory, index, backup_dir=backups, keep=3)
    check("维护会把很旧的记忆转冷存储", report["archived"] >= 1, str(report["archived"]))
    check("维护会合并相似记忆", report["merged"] >= 1, str(report["merged"]))
    check("合并后相似记忆只剩一条",
          len([i for i in memory.all_semantic(kind="preference")]) == 1,
          str([i["content"] for i in memory.all_semantic(kind="preference")]))
    check("执行了 VACUUM", report.get("vacuum") is True, str(report.get("vacuum")))
    check("维护动作写进审计日志",
          any(item["action"] == "maintenance" for item in memory.recent_logs(20)), "")

    # ---- 5. 备份与轮转 ----
    check("生成了备份文件",
          report["backup"].get("path") and pathlib.Path(report["backup"]["path"]).exists(),
          str(report["backup"].get("path")))
    for _ in range(4):
        maintenance.backup(memory, backups, keep=3)
    kept = sorted(backups.glob("*-记忆备份.json"))
    check("旧备份按保留份数自动清理", len(kept) == 3, f"保留 {len(kept)} 份")
    newest = kept[-1]
    payload = newest.read_text(encoding="utf-8")
    check("备份是可读 JSON 且含全表",
          "episodic" in payload and "semantic" in payload and "memory_link" in payload, "")

    # ---- 6. 恢复备份可用 ----
    fresh = Memory(TMP / "fresh.db")
    restored = fresh.import_bundle(newest)
    check("备份能恢复到新库",
          restored["episodic"] >= 1 and restored["semantic"] >= 1, str(restored))
    fresh.close()

    check("报告能打印成人话", "衰退体检" in maintenance.format_report(report), "")

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
