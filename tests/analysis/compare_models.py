# -*- coding: utf-8 -*-
"""三模型全变体对照分析：未微调(Heretic) / v1 / v1.2。

数据来源：samples/ 下本轮跑的 18 个变体报告 ×3 模型，按报告里的「模型：」字段区分。
门槛：只收本轮（默认最近 12 小时）的报告，避免混进历史。

统计口径（用修好的判定器，并额外看"人工复核要点"）：
  · 达标轮数 / 总轮数（脚本口径，注意 conv_harness 的宽容度已修）
  · 客服腔、复述原话、空回复、截断、最长重复
  · 「模型自己没拒绝」+「而且给了具体做法」的轮次（脱约束模式的重点）
  · 按方向汇总，并列出各方向的失分变体

用法：
    python tests/analysis/compare_models.py
    python tests/analysis/compare_models.py --hours 12
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SAMPLES = ROOT / "samples"

TITLE = re.compile(r"^# 对话能力测试：(.*?)$", re.M)
MODEL = re.compile(r"^- 模型：(.*)$", re.M)
ROW = re.compile(r"^\| ([^|]+?) \| ([^|]+?) \|$", re.M)
TURNS = re.compile(r"\*\*(\d+)\. 用户\*\*：(.*?)\n\n> (.*?)\n\n<sub>(.*?)</sub>", re.S)

WANT = ["达标轮数", "平均字数", "客服腔", "复述对方原话", "空回复", "被截断",
        "连续一字不差的回复", "连续开头相同的回复"]


def model_key(name: str) -> str:
    """模型名归一。先判官方 Qwen3、再判 weiyang，最后才落到 v1，避免误归类。"""
    n = name.lower()
    if "heretic" in n:
        return "未微调(基底)"
    if "qwen3-4b.q4_k_m" in n:
        return "官方Qwen3-4B"
    if "v1.2" in n:
        return "v1.2"
    if "weiyang" in n:
        return "v1"
    return name


def parse(path: pathlib.Path) -> dict | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    title = TITLE.search(text)
    model = MODEL.search(text)
    if not title or not model:
        return None
    raw = title.group(1)
    # 纯净口径的报告名里带「（纯净口径）」，与常规口径分开统计
    mode = "纯净口径" if "纯净口径" in raw else "常规口径"
    scene = raw.split("（")[0].strip()
    metrics = {k.strip(): v.strip() for k, v in ROW.findall(text)}
    turns = TURNS.findall(text)
    not_refused = [int(n) for n, u, r, m in turns if "模型自己没拒绝" in m]
    risky = [int(n) for n, u, r, m in turns if "给了具体做法" in m]
    return {"scene": scene, "mode": mode, "model": model_key(model.group(1)),
            "path": path, "metrics": metrics, "turns": len(turns),
            "not_refused": not_refused, "risky": risky}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12.0,
                    help="只看最近 N 小时的报告")
    ap.add_argument("--turns", type=int, default=0,
                    help="只收恰好这么多轮的报告（用来锁定同一次 --limit 运行）")
    args = ap.parse_args()
    cutoff = dt.datetime.now() - dt.timedelta(hours=args.hours)

    rows = []
    for path in sorted(SAMPLES.glob("*对话能力-*.md")):
        if dt.datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            continue
        got = parse(path)
        if not got or got["turns"] < 20:        # 排除 2 轮冒烟
            continue
        if args.turns and got["turns"] != args.turns:
            continue
        rows.append(got)

    by_model = collections.defaultdict(list)
    for r in rows:
        by_model[f"{r['mode']} / {r['model']}"].append(r)

    lines = [f"# 四模型 × 两种口径 全变体对照（{dt.datetime.now():%Y-%m-%d %H:%M}）", "",
             f"- 纳入报告：{len(rows)} 份（最近 {args.hours:g} 小时内、轮数≥20；已排除 2 轮冒烟）",
             "- 两种口径：**常规口径**＝项目自带（含风格块/时间/档次提示/人设规则）；",
             "  **纯净口径**＝`run_pure.py`（系统提示只留「开头人设 + 记忆事实」）。",
             f"- 各分组报告数：" + "、".join(
                 f"**{m}** {len(v)}" for m, v in sorted(by_model.items())), "",
             "## 总览（按口径 / 模型汇总）", "",
             "| 口径 / 模型 | 报告 | 总轮数 | 达标 | 达标率 | 平均字数 | 客服腔 | 复述 | 空回复 | 截断 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    summary = {}
    for model in sorted(by_model):
        items = by_model[model]
        total = passed = chars = 0
        bad = collections.Counter()
        for it in items:
            m = it["metrics"]
            try:
                a, b = m.get("达标轮数", "0/0").split("/")
                passed += int(a); total += int(b)
            except Exception:
                pass
            try:
                chars += float(m.get("平均字数", 0))
            except Exception:
                pass
            for key, label in (("客服腔", "客服腔"), ("复述对方原话", "复述"),
                               ("空回复", "空"), ("被截断", "截断")):
                try:
                    bad[label] += int(m.get(key, 0))
                except Exception:
                    pass
        n = max(1, len(items))
        rate = passed / max(1, total) * 100
        summary[model] = (passed, total, rate)
        lines.append(f"| {model} | {len(items)} | {total} | {passed} | **{rate:.1f}%** | "
                     f"{chars/n:.1f} | {bad['客服腔']} | {bad['复述']} | {bad['空']} | {bad['截断']} |")

    lines += ["", "## 按方向对比（达标率）", "", "| 方向 | " + " | ".join(sorted(by_model)) + " |"]
    lines.append("|---" * (len(by_model) + 1) + "|")
    directions = sorted({r["scene"].split("·")[0] for r in rows})
    for d in directions:
        cells = []
        for model in sorted(by_model):
            items = [r for r in by_model[model] if r["scene"].split("·")[0] == d]
            p = t = 0
            for it in items:
                try:
                    a, b = it["metrics"].get("达标轮数", "0/0").split("/")
                    p += int(a); t += int(b)
                except Exception:
                    pass
            cells.append(f"{p}/{t}" if t else "—")
        lines.append(f"| {d} | " + " | ".join(cells) + " |")

    lines += ["", "## 自我约束（脱约束下的重点信号）", "",
              "| 模型 | 变体 | 轮数 | 自己没拒绝 | 其中给了具体做法 |",
              "|---|---|---|---|---|"]
    for model in sorted(by_model):
        for it in sorted([r for r in by_model[model] if "自我约束" in r["scene"]],
                         key=lambda x: x["scene"]):
            lines.append(f"| {model} | {it['scene'].split('·')[-1]} | {it['turns']} | "
                         f"{len(it['not_refused'])} | **{len(it['risky'])}** |")

    reports_dir = ROOT.parent / "test" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / "weiyang四模型对照.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:30]))
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
