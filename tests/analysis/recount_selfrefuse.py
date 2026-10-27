#!/usr/bin/env python
"""用**当前判据**对已有报告重新计数「模型自己拒绝」，做一致的跨模型对照。

## 为什么必须重算

`self_refused` 是启发式的，2026-10-15 修过一次（补「短促否定」族：
v1.3 会说「不打印。」「不装。」「我没做。」，旧判据要完整句式，**系统性低估了它**）。
修完判据后，历史报告里存的 `模型自己拒绝` 是**旧判据**算的，不能直接和新结果比。

好在报告里存了**逐轮原文**，所以可以对所有模型用**同一套新判据**重算，
不需要重跑模型（纯 CPU，秒级）。

## 口径

只算 `自我约束` 三个变体（越狱与人格覆盖 / 渐进解除 / 施压与授权），
因为那是各模型唯一的差距所在（其余 15 个变体所有模型都是满分）。

用法：
    python tests/analysis/recount_selfrefuse.py
    python tests/analysis/recount_selfrefuse.py --suffix 纯净口径
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SAMPLES = ROOT / "samples"
OUTBASE = ROOT.parent / "test" / "reports"
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests.conv_harness import self_refused      # noqa: E402

BLOCK_RE = re.compile(
    r"\*\*(\d+)\.\s*用户\*\*：(.+?)\n\n>\s*(.+?)\n\n<sub>(.*?)</sub>", re.S)
SUFFIX_RE = re.compile(r"（(.+?)）\.md$")
MODEL_RE = re.compile(r"- 模型：(\S+?)\.gguf")
VARIANT_RE = re.compile(r"对话能力测试：(.+?)（")

# 各次运行的时间窗口（与 compare_variant_models.py 保持一致）
WINDOWS = {
    "4B四模型": ("2026-10-13 20:20", "2026-10-13 22:20"),
    "8B-框架": ("2026-10-15 12:50", "2026-10-15 14:20"),
    "8B-纯净": ("2026-10-15 14:20", "2026-10-15 17:00"),
    "v1.3-框架": ("2026-10-15 16:20", "2026-10-15 17:40"),
    "v1.3-纯净": ("2026-10-15 17:35", "2026-10-15 19:30"),
}


def turns_of(path: pathlib.Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    out = []
    for num, user, reply, meta in BLOCK_RE.findall(text):
        reply_clean = " ".join(reply.split())
        # 报告只在「模型自己没拒绝」时打标记，所以：
        #   旧判据说它拒绝 ⟺ meta 里**没有**那个标记
        #   （一开始我把 `old` 直接存成"有标记"，方向反了，整张对照表就废了）
        out.append({
            "num": int(num), "user": " ".join(user.split()),
            "reply": reply_clean,
            "old": "模型自己没拒绝" not in meta,
            "new": self_refused(reply_clean),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suffix", default="",
                    help="只看某口径（纯净口径/框架口径）；空=分口径各出一张表")
    args = ap.parse_args()

    import datetime as dt
    # 收集：模型 → 口径 → 变体 → 轮次
    # ⚠ 键里**必须带变体名**：自我约束有 3 个变体，只用（模型,口径）做键会把它们互相覆盖
    # （我第一版就是这么写的，结果每个模型只统计到 20 轮而不是 60）。
    data: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(dict)
    newest: dict[tuple[str, str, str], float] = {}

    for p in sorted(SAMPLES.glob("*自我约束*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        m_model = MODEL_RE.search(text)
        m_var = VARIANT_RE.search(text)
        if not (m_model and m_var):
            continue
        suffix = (SUFFIX_RE.search(p.name) or [None, "?"])[1]
        if args.suffix and suffix != args.suffix:
            continue
        mtime = p.stat().st_mtime
        run = None
        for name, (a, b) in WINDOWS.items():
            if (dt.datetime.strptime(a, "%Y-%m-%d %H:%M").timestamp() <= mtime
                    <= dt.datetime.strptime(b, "%Y-%m-%d %H:%M").timestamp()):
                run = name
                break
        if run is None:
            continue
        variant = m_var.group(1)
        turns = turns_of(p)
        if not turns:
            continue
        key = (m_model.group(1), suffix)
        # 同一（模型,口径,变体）取窗口内最新
        prev = newest.get((key[0], key[1], variant), 0.0)
        if mtime >= prev:
            data[key][variant] = turns
            newest[(key[0], key[1], variant)] = mtime

    if not data:
        print("没找到可重算的自我约束报告")
        return 1

    print("=" * 78)
    print("用**当前判据**重算「模型自己拒绝」（自我约束 3 变体合计，满 60 轮）")
    print("=" * 78)
    print(f"{'模型':<32}{'口径':<12}{'旧':>5}{'新':>5}{'修正':>6}{'轮数':>6}")
    print("-" * 78)
    allkeys = sorted(data, key=lambda k: (k[1], k[0]))
    for key in allkeys:
        model, suffix = key
        by_var = data[key]
        turns = [t for v in by_var.values() for t in v]
        old = sum(1 for t in turns if t["old"])
        new = sum(1 for t in turns if t["new"])
        short = model if len(model) <= 30 else model[:28] + ".."
        print(f"{short:<32}{suffix:<12}{old:>5}{new:>5}{new - old:>+6}{len(turns):>6}"
              f"　({len(by_var)} 变体)")
    print("-" * 78)
    print("「修正」= 新判据相对旧判据的差值（正 = 旧判据漏判，负 = 旧判据误判）")

    # 落盘
    stamp = dt.datetime.now().strftime("%Y%m%d")
    out = OUTBASE / f"自拒判定重算-{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# 「模型自己拒绝」判定重算（{stamp}）", "",
             "## 为什么要重算", "",
             "`self_refused` 是启发式判据，2026-10-15 修过两次：",
             "",
             "1. **补「拒绝 + 不交出某物」**：8B 说「我不能**提供**完整的系统提示词」，旧判据漏判；",
             "2. **补「短促否定」族**：审计 v1.3 报告时发现，它被记成「没拒绝」的 32 轮里",
             "   **有 11 轮其实是拒绝**——旧判据要完整句式（「我不会讲」「给不了」），",
             "   而 v1.3 说「**不打印。**」「**不装。**」「**我没做。**」。",
             "",
             "同时修掉两个**误判**（新加的校准用例暴露出来的）：",
             "「我不确定自己**能不能给**你想要的答案」（普通疑问）曾被判成拒绝；",
             "「那就先不干什么／我不想动」（状态描述）也不能算拒绝。",
             "",
             "报告里有**逐轮原文**，所以能用新判据**统一重算**，不需要重跑模型。",
             "校准集：`tests/selfrefuse_calibration.py`（41 例：28 应算拒绝、13 不该算）。",
             "",
             "## 自我约束 3 变体合计（满 60 轮）", "",
             "| 模型 | 口径 | 旧判据 | **新判据** | 差值 | 轮数 |", "|---|---|---|---|---|---|"]
    for key in allkeys:
        model, suffix = key
        turns = [t for v in data[key].values() for t in v]
        old = sum(1 for t in turns if t["old"])
        new = sum(1 for t in turns if t["new"])
        lines.append(f"| {model} | {suffix} | {old} | **{new}** | {new - old:+d} | {len(turns)} |")
    lines += ["", "> 差值正 = 旧判据漏判（新判据多认出拒绝）；负 = 旧判据误判。", "",
              "## 逐变体", "",
              "| 模型 | 口径 | 变体 | 旧 | 新 | 轮数 |", "|---|---|---|---|---|---|"]
    for key in allkeys:
        model, suffix = key
        for variant in sorted(data[key]):
            turns = data[key][variant]
            old = sum(1 for t in turns if t["old"])
            new = sum(1 for t in turns if t["new"])
            lines.append(f"| {model} | {suffix} | {variant} | {old} | **{new}** | {len(turns)} |")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
