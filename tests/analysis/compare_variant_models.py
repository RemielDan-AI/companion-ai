#!/usr/bin/env python
"""跨模型对照：同一套变体、同轮数，把多个模型的报告并成一张表。

用途（2026-10-15）：判断新微调版 **weiyang-v1.3** 相对 v1.2 / v1 / 未微调 / 官方 4B / 8B
到底有没有提升。

## 怎么区分模型

报告文件名里**只有变体名和口径**，没有模型名，所以本脚本从报告**正文**里读
`- 模型：xxx.gguf`。这样对老报告（无模型标签）也有效。

## 怎么区分"哪一次运行"

同一模型同一变体可能有多份报告（重跑过）。按 `--window` 给的时间窗口筛，
每个变体取**窗口内最新**的一份。这样能精确对应"某一次运行"。
各次运行的时间窗口（实测）：

    2026-10-13 20:20–22:20   四个 4B 模型（官方 / 未微调 / v1 / v1.2）
    2026-10-15 11:55–12:16   8B 的 6 轮快速体检（**不是** 20 轮，会被轮数条件排除）
    2026-10-15 12:55–14:15   8B 框架口径 20 轮
    2026-10-15 14:27–16:20   8B 纯净口径 20 轮
    （v1.3 跑完后按实际时间加窗口）

用法：
    python tests/analysis/compare_variant_models.py \
        --model 官方=Qwen3-4B.Q4_K_M --model 未微调=GPT-5-Distill \
        --model v1=weiyang-v1-Q4_K_M --model v1.2=weiyang-v1.2-Q4_K_M \
        --model v1.3=weiyang-v1.3-Q4_K_M \
        --since "2026-10-15 16:20"
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SAMPLES = ROOT / "samples"
OUTBASE = ROOT.parent / "test" / "reports"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DIRECTIONS = ("日常陪伴", "情绪支持", "记忆连贯", "决策倾诉", "技能边界", "自我约束")

# 各次运行的时间窗口（实测）。用 --window 名字调用，省得手打时间出错。
# 目的：让"哪一批报告"有唯一定义——同名报告太多，靠人记时间迟早出错。
WINDOWS = {
    # 2026-10-13 四个 4B 模型的**纯净口径** 20 轮（90 份，全部是"纯净口径"）
    "4B四模型-纯净": ("2026-10-13 20:20", "2026-10-13 22:20"),
    # 2026-10-15 8B：框架口径 20 轮
    "8B-框架": ("2026-10-15 12:50", "2026-10-15 14:20"),
    # 2026-10-15 8B：纯净口径 20 轮
    "8B-纯净": ("2026-10-15 14:20", "2026-10-15 17:00"),
}
# 变体名 → 模型名（排除"文件名里恰好包含模型名"的误匹配）
MODEL_RE = re.compile(r"模型：(\S+?)\.gguf")
VARIANT_RE = re.compile(r"对话能力测试：(.+?)（(.+?)）")
PASSED_RE = re.compile(r"达标轮数 \| (\d+)/(\d+)")
CHARS_RE = re.compile(r"平均字数 \| ([\d.]+)")
SECS_RE = re.compile(r"平均耗时 \| ([\d.]+)s")


def parse(path: pathlib.Path) -> dict | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    m_model = MODEL_RE.search(text)
    m_var = VARIANT_RE.search(text)
    m_pass = PASSED_RE.search(text)
    if not (m_model and m_var and m_pass):
        return None
    return {
        "path": path, "mtime": path.stat().st_mtime,
        "model": m_model.group(1),
        "variant": m_var.group(1), "suffix": m_var.group(2),
        "passed": int(m_pass.group(1)), "turns": int(m_pass.group(2)),
        "chars": float((CHARS_RE.search(text) or ["", "0"])[1]),
        "secs": float((SECS_RE.search(text) or ["", "0"])[1]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", default=[],
                    metavar="别名=模型名片段",
                    help="可多次给；模型名片段匹配报告正文里的模型名")
    ap.add_argument("--suffix", default="", help="只看某个口径后缀（如 纯净口径）；空=全收（会警告）")
    ap.add_argument("--turns", type=int, default=20)
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--window", default="",
                    help="命名时间窗口（省得手打时间）：" + "、".join(WINDOWS))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if not args.model:
        print("至少给一个 --model 别名=模型名片段")
        return 1
    if args.window:
        if args.window not in WINDOWS:
            print(f"未知窗口「{args.window}」，可用：" + "、".join(WINDOWS))
            return 1
        args.since, args.until = WINDOWS[args.window]
    if not args.since:
        print("要给 --since（或 --window）")
        return 1
    since = dt.datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp()
    until = (dt.datetime.strptime(args.until, "%Y-%m-%d %H:%M").timestamp()
             if args.until else float("inf"))

    pool: list[dict] = []
    for p in SAMPLES.glob("*.md"):
        info = parse(p)
        if not info or info["turns"] != args.turns:
            continue
        if not (since <= info["mtime"] <= until):
            continue
        if args.suffix and not info["suffix"].endswith(args.suffix):
            # 用 endswith 而不是等号：带模型标签的报告后缀是「v1.3纯净口径」，
            # 请求 `--suffix 纯净口径` 时也要能收进来（等号匹配会把它排除）。
            continue
        pool.append(info)
    print(f"窗口内 {args.turns} 轮报告：{len(pool)} 份")

    wanted = []
    for spec in args.model:
        if "=" not in spec:
            print(f"  ✗ 格式应为 别名=模型名片段：{spec}")
            return 1
        alias, frag = spec.split("=", 1)
        wanted.append((alias, frag))

    # 每个（别名, 变体）取窗口内最新
    got: dict[tuple[str, str], dict] = {}
    for alias, frag in wanted:
        for info in sorted(pool, key=lambda x: x["mtime"]):
            if frag.lower() in info["model"].lower():
                got[(alias, info["variant"])] = info

    variants = sorted({v for (_a, v) in got},
                      key=lambda v: (DIRECTIONS.index(v.split("·")[0])
                                     if v.split("·")[0] in DIRECTIONS else 99, v))
    print(f"覆盖到 {len(variants)} 个变体 / {len(wanted)} 个模型别名")
    print()

    lines = [f"# 跨模型变体对照（{dt.datetime.now():%Y-%m-%d}）", ""]
    lines.append(f"- 轮数：每变体 {args.turns} 轮；口径后缀筛选：**{args.suffix or '⚠ 未限定（可能混合两个口径）'}**")
    lines.append(f"- 时间窗口：{args.since} 起" + (f" 至 {args.until}" if args.until else ""))
    lines.append("")
    if not args.suffix:
        lines.append("> ⚠ 没有限定口径时，同名的纯净/框架报告会被混着取——**结论不可用**。"
                     "请显式给 `--suffix 纯净口径` 或 `--suffix 框架口径`。")
        lines.append("")
    # 总表
    hdr = "| 变体 | " + " | ".join(a for a, _ in wanted) + " |"
    lines += [hdr, "|---" * (len(wanted) + 1) + "|"]
    totals: dict[str, list[int]] = {a: [0, 0] for a, _ in wanted}
    for v in variants:
        cells = []
        for alias, _f in wanted:
            info = got.get((alias, v))
            if not info:
                cells.append("—")
                continue
            totals[alias][0] += info["passed"]
            totals[alias][1] += info["turns"]
            cells.append(f"{info['passed']}/{info['turns']}")
        lines.append(f"| {v} | " + " | ".join(cells) + " |")
    cells = []
    for alias, _f in wanted:
        p, t = totals[alias]
        cells.append(f"**{p}/{t} = {p / t * 100:.1f}%**" if t else "—")
    lines.append("| **合计** | " + " | ".join(cells) + " |")
    lines.append("")

    # 逐模型均字数/耗时
    lines += ["| 指标 | " + " | ".join(a for a, _ in wanted) + " |",
              "|---" * (len(wanted) + 1) + "|"]
    for key, label, unit in (("chars", "平均字数", " 字"), ("secs", "平均耗时", "s")):
        row = []
        for alias, _f in wanted:
            vals = [got[(alias, v)][key] for v in variants if (alias, v) in got]
            row.append(f"{sum(vals) / len(vals):.1f}{unit}" if vals else "—")
        lines.append(f"| {label} | " + " | ".join(row) + " |")
    lines.append("")

    out = pathlib.Path(args.out) if args.out else OUTBASE / f"跨模型变体对照-{dt.datetime.now():%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    # 终端里也打一份，便于立刻看
    print("| 变体 | " + " | ".join(a for a, _ in wanted) + " |")
    for v in variants + ["**合计**"]:
        if v == "**合计**":
            cells = [f"{totals[a][0]}/{totals[a][1]}" if totals[a][1] else "—"
                     for a, _ in wanted]
        else:
            cells = []
            for alias, _f in wanted:
                info = got.get((alias, v))
                cells.append(f"{info['passed']}/{info['turns']}" if info else "—")
        print(f"| {v} | " + " | ".join(cells) + " |")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
