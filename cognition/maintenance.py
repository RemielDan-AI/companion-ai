"""长期运行维护（项目改进v1 改进 28）。

四件事，可以一键跑，也可以分开看：
1. **整理压缩**：把长期低重要度的记忆转冷存储、合并相似语义记忆、VACUUM 收缩数据库；
2. **检索质量评估**：拿库里已有的记忆自身当查询，看能不能被检索回来（自召回率），
   顺带统计关键词兜底/向量检索各占多少；
3. **衰退机制体检**：看有效重要度分布、冷存储比例、有没有"全都不衰减"或"全都贴地板"
   这类明显不合理的状态；
4. **定期备份**：导出整库到 backups/，并按保留份数自动清理旧备份。

不写数据库之外的任何东西；所有动作都写审计日志（action=maintenance）。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

from cognition import decay as decay_engine
from cognition.memory import Memory

BACKUP_DIR = pathlib.Path(__file__).resolve().parent.parent / "backups"


def backup(memory: Memory, backup_dir=BACKUP_DIR, keep: int = 5) -> dict:
    """导出整库并保留最近 keep 份（旧的自动删）。"""
    backup_dir = pathlib.Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    # 文件名带上毫秒：连着跑多次备份时不会被同一秒覆盖掉
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    path = backup_dir / f"{stamp}-记忆备份.json"
    result = memory.export_bundle(path)
    files = sorted(backup_dir.glob("*-记忆备份.json"))
    removed = []
    for old in files[:-keep] if keep > 0 else []:
        try:
            old.unlink()
            removed.append(old.name)
        except Exception:
            continue
    return {"path": str(path), "counts": result["counts"], "removed": removed,
            "kept": len(files) - len(removed)}


def quality(memory: Memory, index=None, sample: int = 8) -> dict:
    """检索质量：用自己的记忆当查询，看能不能被找回来。"""
    from engine.retrieve import retrieve

    candidates: list[tuple[str, str]] = []
    for item in memory.all_semantic()[:sample]:
        candidates.append(("semantic", str(item.get("content") or "")))
    for item in memory.recent_episodes(limit=sample):
        if item.get("summary"):
            candidates.append(("episodic", str(item["summary"])))
    if not candidates:
        return {"checked": 0, "hit": 0, "rate": 0.0, "modes": {}, "misses": []}

    hit = 0
    modes: dict[str, int] = {}
    misses: list[str] = []
    for kind, query in candidates[:sample * 2]:
        try:
            result = retrieve(query, memory, index, record_access=False)
        except Exception as exc:
            misses.append(f"{kind}:{query[:16]}（检索失败 {exc}）")
            continue
        modes[result.get("mode", "?")] = modes.get(result.get("mode", "?"), 0) + 1
        found = any(query[:12] in str(item.get("content") or "")
                    or query[:12] in str(item.get("summary") or "")
                    for item in (result.get("semantic") or []) + (result.get("episodes") or []))
        hit += 1 if found else 0
        if not found:
            misses.append(f"{kind}:{query[:16]}")
    checked = min(len(candidates), sample * 2)
    return {"checked": checked, "hit": hit,
            "rate": round(hit / checked, 3) if checked else 0.0,
            "modes": modes, "misses": misses[:5]}


def decay_health(memory: Memory, rules: dict | None = None) -> dict:
    """衰退机制体检：分布合理吗？有没有明显失衡。"""
    rules = rules or decay_engine.load_rules()
    rows: list[dict] = []
    for item in memory.search_texts(limit=100000):
        entry = dict(item)
        entry["effective"] = decay_engine.decayed_importance(entry, rules)
        rows.append(entry)
    if not rows:
        return {"total": 0, "warnings": ["库里还没有记忆，没法体检"], "buckets": {}}
    values = [row["effective"] for row in rows]
    floor = float(rules.get("floor", 0.05))
    buckets = {"贴地板(<0.1)": 0, "偏冷(0.1–0.5)": 0, "中等(0.5–2)": 0, "高(≥2)": 0}
    for value in values:
        if value < 0.1:
            buckets["贴地板(<0.1)"] += 1
        elif value < 0.5:
            buckets["偏冷(0.1–0.5)"] += 1
        elif value < 2:
            buckets["中等(0.5–2)"] += 1
        else:
            buckets["高(≥2)"] += 1
    cold = sum(1 for row in rows if int(row.get("archived") or 0) == 1)
    warnings: list[str] = []
    if buckets["贴地板(<0.1)"] / len(rows) > 0.6:
        warnings.append("六成以上记忆已贴到重要度下限——半衰期可能设得太短")
    if buckets["高(≥2)"] == len(rows):
        warnings.append("没有任何记忆在衰减——检查 decay.yaml 的 enabled 与半衰期")
    if cold / len(rows) > 0.7:
        warnings.append("七成以上进了冷存储——archive_threshold 可能偏高")
    if not warnings:
        warnings.append("分布正常")
    return {"total": len(rows), "cold": cold,
            "cold_ratio": round(cold / len(rows), 3),
            "avg_effective": round(sum(values) / len(values), 3),
            "max_effective": round(max(values), 3),
            "min_effective": round(min(values), 3),
            "buckets": buckets, "warnings": warnings}


def run(memory: Memory, index=None, backup_dir=BACKUP_DIR, keep: int = 5,
        vacuum: bool = True, check_only: bool = False) -> dict:
    """一键维护。check_only=True 时只体检与评估，不改任何数据。"""
    report: dict = {"time": dt.datetime.now().isoformat(timespec="seconds"),
                    "check_only": check_only}
    if check_only:
        report["quality"] = quality(memory, index)
        report["decay"] = decay_health(memory)
        return report

    moved = memory.archive_cold()
    # 走带快照的事务版（借鉴 Cyrene-Agent）：合错了能回滚，不再是就地改库
    compression = memory.compress_transaction()
    merged = list(range(int(compression.get("merged") or 0)))
    report["archived"] = len(moved)
    report["merged"] = len(merged)
    report["compression_log_id"] = compression.get("log_id", 0)
    if vacuum:
        try:
            memory.conn.execute("VACUUM")
            report["vacuum"] = True
        except Exception as exc:
            report["vacuum"] = f"跳过：{exc}"
    report["quality"] = quality(memory, index)
    report["decay"] = decay_health(memory)
    try:
        report["backup"] = backup(memory, backup_dir, keep)
    except Exception as exc:
        report["backup"] = {"error": str(exc)}
    summary = (f"转冷 {report['archived']}、合并 {report['merged']}、"
               f"检索自召回合 {report['quality']['hit']}/{report['quality']['checked']}、"
               f"备份 {report.get('backup', {}).get('path', '失败')}")
    memory.log("maintenance", "all", None, summary)
    report["summary"] = summary
    return report


def format_report(report: dict) -> str:
    """给人看的维护报告。"""
    lines = [f"维护时间：{report.get('time', '')}"
             + ("（只体检，没有改动）" if report.get("check_only") else "")]
    if "archived" in report:
        lines.append(f"整理：转冷存储 {report['archived']} 条、合并相似记忆 {report['merged']} 条"
                     f"、VACUUM {'完成' if report.get('vacuum') is True else report.get('vacuum')}")
    q = report.get("quality") or {}
    if q:
        lines.append(f"检索质量：自召回 {q.get('hit', 0)}/{q.get('checked', 0)}"
                     f"（命中率 {q.get('rate', 0):.0%}，模式 {q.get('modes', {})}）")
        for miss in q.get("misses") or []:
            lines.append(f"    漏掉：{miss}")
    d = report.get("decay") or {}
    if d:
        lines.append(f"衰退体检：{d.get('total', 0)} 条，冷存储 {d.get('cold', 0)} 条"
                     f"（{d.get('cold_ratio', 0):.0%}），平均有效重要度 "
                     f"{d.get('avg_effective', 0)}")
        lines.append(f"    分布：{d.get('buckets', {})}")
        for warning in d.get("warnings") or []:
            lines.append(f"    ⚠ {warning}")
    b = report.get("backup") or {}
    if b:
        lines.append(f"备份：{b.get('path') or b.get('error')}"
                     + (f"（清掉旧的 {len(b.get('removed') or [])} 份）" if b.get("removed")
                        else ""))
    if report.get("summary"):
        lines.append(f"一句话：{report['summary']}")
    return "\n".join(lines)
