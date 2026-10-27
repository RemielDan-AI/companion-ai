#!/usr/bin/env python
"""把变体测试的结果汇总并存档（**按模型标签**筛选）。

## 为什么要"精确筛选"而不是全收

`companion-ai/samples/` 里同一变体名的报告**会有很多份**（实测）：
    · 2026-10-13 的旧报告（20 轮）
    · 2026-10-15 上午的 6 轮快速体检
    · 2026-10-15 本次的 20 轮全量
    · 不同模型（8B / v1.3）跑同一套变体
文件名只差时间戳与模型标签，肉眼很容易翻错。所以按
**报告后缀（模型标签 + 口径）+ 时间窗口 + 轮数**筛。

## 输出

    test/reports/<标签>变体测试-<日期>/         本次报告副本（按口径分目录）
    test/reports/<标签>变体测试-<日期>/汇总.md   各口径逐变体表 + 口径对照

用法：
    python tests/analysis/summarize_variants.py --tag ""     --since-framework "2026-10-15 12:50" --since-pure "2026-10-15 14:20"
    python tests/analysis/summarize_variants.py --tag v1.3   --since-framework "..." --since-pure "..."
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SAMPLES = ROOT / "samples"
OUTBASE = ROOT.parent / "test" / "reports"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 报告正文里的模型名：Qwen3 系（官方/8B）与自训版（weiyang-*）都要认。
# ⚠ 一开始只写了 "Qwen3"，结果自训版的报告全被筛掉（它叫 weiyang-v1.3-Q4_K_M）。
MODEL_MARK = ("Qwen3", "weiyang", "Heretic")
DIRECTIONS = ("日常陪伴", "情绪支持", "记忆连贯", "决策倾诉", "技能边界", "自我约束")

FIELDS = {
    "turns": r"轮数：(\d+)",
    "passed": r"达标轮数 \| (\d+)/(\d+)",
    "avg_chars": r"平均字数 \| ([\d.]+)",
    "avg_sec": r"平均耗时 \| ([\d.]+)s",
    "service": r"客服腔 \| (\d+)",
    "echo": r"复述对方原话 \| (\d+)",
    "truncated": r"被截断 \| (\d+)",
    "empties": r"空回复 \| (\d+)",
    "errors": r"生成出错 \| (\d+)",
    "identical": r"连续一字不差的回复 \| (\d+)",
}


def parse(path: pathlib.Path) -> dict | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not any(m in text for m in MODEL_MARK):
        return None
    out: dict = {"path": path, "name": path.name}
    for key, pat in FIELDS.items():
        m = re.search(pat, text)
        if not m:
            continue
        if key == "passed":
            out["passed"] = int(m.group(1))
        else:
            val = m.group(1)
            out[key] = int(val) if key in ("service", "echo", "truncated", "empties",
                                           "errors", "turns", "identical") else float(val)
    m = re.search(r"对话能力测试：(.+?)（(.+?)）", text)
    if not m:
        return None
    out["variant"] = m.group(1)
    out["suffix"] = m.group(2)          # 如「框架口径」「v1.3框架口径」
    # 报告正文里的真实模型名，用来核对标签没标错
    m = re.search(r"模型：(\S+?)\.gguf", text)
    out["model"] = m.group(1) if m else "?"
    return out


def collect(since: dt.datetime, suffix: str, limit: int) -> list[dict]:
    rows = []
    for p in SAMPLES.glob("*.md"):
        if f"（{suffix}）" not in p.name:
            continue
        if p.stat().st_mtime < since.timestamp():
            continue
        info = parse(p)
        if not info or info.get("turns") != limit:
            continue
        rows.append(info)

    def sort_key(r: dict):
        d = r["variant"].split("·")[0]
        return (DIRECTIONS.index(d) if d in DIRECTIONS else 99, r["variant"])

    best: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x["path"].stat().st_mtime):
        best[r["variant"]] = r      # 重跑取最新
    return sorted(best.values(), key=sort_key)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="", help="模型标签（如 v1.3）；空 = 无标签那批")
    ap.add_argument("--since-framework", default="2026-10-15 12:50")
    ap.add_argument("--since-pure", default="2026-10-15 14:20")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--outdir", default="")
    args = ap.parse_args()

    since = {"框架": dt.datetime.strptime(args.since_framework, "%Y-%m-%d %H:%M"),
             "纯净": dt.datetime.strptime(args.since_pure, "%Y-%m-%d %H:%M")}
    suffixes = {m: f"{args.tag}{m}口径" for m in ("框架", "纯净")}
    data = {m: collect(since[m], suffixes[m], args.limit) for m in ("框架", "纯净")}

    for m, rows in data.items():
        print(f"{m}口径（后缀「{suffixes[m]}」）：{len(rows)}/18 个变体"
              + (f"　模型：{rows[0]['model']}" if rows else ""))
    if not any(data.values()):
        print("没收到任何报告——检查标签/时间窗口/轮数")
        return 1

    stamp = dt.datetime.now().strftime("%Y%m%d")
    label = args.tag or "8B"
    outdir = pathlib.Path(args.outdir) if args.outdir else OUTBASE / f"{label}变体测试-{stamp}"
    copied = 0
    for m, rows in data.items():
        d = outdir / f"{m}口径"
        d.mkdir(parents=True, exist_ok=True)
        for r in rows:
            shutil.copy2(r["path"], d / r["path"].name)
            copied += 1
    print(f"已复制 {copied} 份报告 → {outdir}")

    model_name = next((r["model"] for rows in data.values() for r in rows), "?")
    lines = [f"# {label} 变体测试汇总（{stamp}）", "",
             f"- 模型：`{model_name}.gguf`",
             f"- 规模：18 变体 × {args.limit} 轮"
             f" × {sum(1 for m in data if data[m])} 口径",
             "- 报告原件也保留在 `companion-ai/samples/`；本目录是同一批的副本，便于查阅",
             ""]
    for m in ("框架", "纯净"):
        rows = data[m]
        if not rows:
            continue
        turns = sum(r.get("turns", 0) for r in rows)
        passed = sum(r.get("passed", 0) for r in rows)
        chars = sum(r.get("avg_chars", 0) for r in rows) / len(rows)
        secs = sum(r.get("avg_sec", 0) for r in rows) / len(rows)
        lines += [f"## {m}口径", "",
                  f"**{passed}/{turns} = {passed / turns * 100:.1f}%**，"
                  f"平均 {chars:.1f} 字 / {secs:.1f}s", "",
                  "| 变体 | 轮数 | 达标 | 达标率 | 平均字数 | 平均耗时 | 客服腔 | 复述 | 截断 | 出错 | 一字不差 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            t, ok = r.get("turns", 0), r.get("passed", 0)
            rate = f"{ok / t * 100:.0f}%" if t else "-"
            lines.append(f"| {r['variant']} | {t} | {ok} | {rate} | "
                         f"{r.get('avg_chars', 0):.1f} | {r.get('avg_sec', 0):.1f}s | "
                         f"{r.get('service', 0)} | {r.get('echo', 0)} | "
                         f"{r.get('truncated', 0)} | {r.get('errors', 0)} | "
                         f"{r.get('identical', 0)} |")
        lines.append("")
    fw, pu = data["框架"], data["纯净"]
    if fw and pu:
        lines += ["## 两口径对照", "", "| 指标 | 框架口径 | 纯净口径 | 差异 |",
                  "|---|---|---|---|"]
        for key, label2, unit in (("passed", "达标轮数", ""),
                                  ("avg_chars", "平均字数", " 字"),
                                  ("avg_sec", "平均耗时", "s")):
            a = sum(r.get(key, 0) for r in fw) if key == "passed" else \
                sum(r.get(key, 0) for r in fw) / len(fw)
            b = sum(r.get(key, 0) for r in pu) if key == "passed" else \
                sum(r.get(key, 0) for r in pu) / len(pu)
            d = b - a
            lines.append(f"| {label2} | {a:.1f}{unit} | {b:.1f}{unit} | "
                         f"{'+' if d >= 0 else ''}{d:.1f}{unit} |")
        lines += ["", "> 「框架」= 完整 system 提示（人设+风格块+时间+档次提示）+ 放宽本地拒绝话术；",
                  "> 「纯净」= system 提示**只留**「开头人设 + 记忆事实」，其余全摘。", ""]

    out = outdir / "汇总.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"汇总 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
