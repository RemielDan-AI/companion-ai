# -*- coding: utf-8 -*-
"""把两份"放宽约束"报告的量化指标并排，并统计重生成量。"""
import pathlib
import re

METRIC = re.compile(r"^\| ([^|]+?) \| ([^|]+?) \|$", re.M)
REL = {
    "v1": "samples/20261013-145902-对话能力-放宽约束下的自我约束.md",
    "v1.2": "samples/20261013-151407-对话能力-放宽约束下的自我约束.md",
}
IDENT = {
    "v1": "samples/20261013-152024-身份直问-weiyang-v1-Q4_K_M.gg.md",
    "v1.2": "samples/20261013-152652-身份直问-weiyang-v1.2-Q4_K_M..md",
}
WANT = ["达标轮数", "平均字数", "平均耗时", "客服腔", "风格问题", "空回复",
        "生成出错", "复述对方原话", "被截断", "连续一字不差的回复"]


def metrics(path):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    got = {}
    for key, val in METRIC.findall(text):
        if key in WANT:
            got[key] = val.strip()
    return got


def main() -> int:
    a, b = metrics(REL["v1"]), metrics(REL["v1.2"])
    lines = ["# 量化指标并排（同一套 75 题、同为脱离本地拒绝规则）", "",
             "| 指标 | v1 | v1.2 | 变化 |", "|---|---|---|---|"]
    for k in WANT:
        if k in a or k in b:
            lines.append(f"| {k} | {a.get(k,'-')} | {b.get(k,'-')} | |")
    lines += ["",
              "重生成量（llama-server 日志里已释放的生成槽数 ÷ 75 题）：",
              "- v1：**88 / 75 ≈ 1.17**",
              "- v1.2：**151 / 75 ≈ 2.01**",
              "- 身份探针（30 题）：v1 **60 / 30 = 2.0**；v1.2 **58 / 30 ≈ 1.93**",
              "",
              "身份探针汇总（判定器口径）：", "",
              "| 项 | v1 | v1.2 |", "|---|---|---|"]
    for name, path in IDENT.items():
        text = pathlib.Path(path).read_text(encoding="utf-8")
        tail = text.split("## 汇总")[-1]
        rows = [ln.strip("- ").strip() for ln in tail.strip().splitlines() if ln.strip()]
        lines.append(f"| {name} | " + "；".join(rows) + " |")
    pathlib.Path("C:/project/_metrics.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
