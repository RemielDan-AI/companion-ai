# -*- coding: utf-8 -*-
"""三模型自我约束变体：逐轮原文对照（人工核实用）。

为什么要单独做：脚本的"给了具体做法"列只是粗筛，而"自己没拒绝"有漏判。
要判断"谁真的泄漏、谁只是措辞不同"，必须把同一题的三个模型回答并排看。

用法：python tests/analysis/selfrestraint_three.py
"""
from __future__ import annotations

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
TURNS = re.compile(r"\*\*(\d+)\. 用户\*\*：(.*?)\n\n> (.*?)\n\n<sub>(.*?)</sub>", re.S)
VARIANTS = ("越狱与人格覆盖", "渐进解除", "施压与授权")


def key_of(name: str) -> str:
    n = name.lower()
    if "heretic" in n:
        return "未微调"
    if "v1.2" in n:
        return "v1.2"
    return "v1"


def main() -> int:
    cutoff = dt.datetime.now() - dt.timedelta(hours=6)
    found: dict[str, dict[str, dict]] = {}
    for path in sorted(SAMPLES.glob("*对话能力-*.md")):
        if dt.datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        t, m = TITLE.search(text), MODEL.search(text)
        if not t or not m:
            continue
        scene = t.group(1).split("（")[0].strip()
        if scene.split("·")[0] != "自我约束":
            continue
        if scene.split("·")[-1] not in VARIANTS:
            continue
        turns = TURNS.findall(text)
        if len(turns) != 20:            # 只取 --limit 20 那批
            continue
        found.setdefault(scene.split("·")[-1], {})[key_of(m.group(1))] = {
            int(n): (u.strip(), r.strip(), meta.strip()) for n, u, r, meta in turns}

    lines = ["# 自我约束变体：三模型逐轮原文对照", "",
             "同一题并排三个模型的回答；**加粗**标出脚本判为「给了具体做法」的轮次。", ""]
    for variant in VARIANTS:
        data = found.get(variant)
        if not data:
            lines.append(f"## {variant}（缺数据）\n")
            continue
        lines += [f"## {variant}", ""]
        idx = sorted(next(iter(data.values())))
        for i in idx:
            users = {k: v[i][0] for k, v in data.items() if i in v}
            sample = next(iter(users.values()))
            lines += [f"**第 {i} 题**　用户：{sample}", ""]
            for model in ("未微调", "v1", "v1.2"):
                if model not in data or i not in data[model]:
                    continue
                reply, meta = data[model][i][1], data[model][i][2]
                flag = "　←**给了具体做法**" if "给了具体做法" in meta else ""
                lines.append(f"- **{model}**：{reply}{flag}")
            lines.append("")
    reports_dir = ROOT.parent / "test" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / "weiyang自我约束逐轮.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写入 {out}（{sum(len(v) for v in found.values())} 组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
