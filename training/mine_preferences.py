"""从测试报告里挖偏好对（prompt / chosen / rejected），给第二轮微调当反例素材。

原理：同一批问法我们跑过好几轮（改一版提示就跑一次），报告里带判定标记
（⚠否认身份 / ⚠越界承诺 / 生硬标签…）。于是：
    rejected = 带 ⚠ 标记的那个回答
    chosen   = 同一问题在别的轮次里没被标记的回答
同一问题拿到一对，就是一条 DPO 样本——**正反例是同一个问题的两种答法**，
模型要学的是分辨，而不是照抄长答案。

用法：
    python training/mine_preferences.py                       # 挖身份直问
    python training/mine_preferences.py --glob "*对话能力*"    # 挖对话报告（暂不做）
输出：training/export/mined_preferences.jsonl
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
OUT = ROOT / "training" / "export" / "mined_preferences.jsonl"

ROW = re.compile(r"^\|\s*(\d+)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$", re.M)
BAD = "⚠"


def parse_report(path: pathlib.Path) -> dict[str, dict]:
    """{问题: {回答, 标记, 模型}}"""
    text = path.read_text(encoding="utf-8", errors="replace")
    model = ""
    match = re.search(r"^# 直问身份与关系边界（模型：([^）]*)）", text, re.M)
    if match:
        model = match.group(1).strip()
    out = {}
    for _num, _cat, question, answer, marks in ROW.findall(text):
        question = question.strip()
        if not question or question == "问题":
            continue
        out[question] = {"answer": answer.strip(), "marks": marks.strip(),
                         "model": model, "file": path.name}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="挖偏好对")
    parser.add_argument("--glob", default="*身份直问*.md")
    args = parser.parse_args()

    reports = sorted(SAMPLES.glob(args.glob), key=lambda p: p.stat().st_mtime)
    if not reports:
        print("没找到报告")
        return 1
    print(f"读入 {len(reports)} 份报告：")
    for path in reports:
        print("  " + path.name)

    by_question: dict[str, list[dict]] = {}
    for path in reports:
        for question, item in parse_report(path).items():
            item = dict(item)
            item["question"] = question
            by_question.setdefault(question, []).append(item)

    pairs = []
    for question, items in by_question.items():
        bad = [x for x in items if BAD in x["marks"]]
        good = [x for x in items if BAD not in x["marks"]
                and "生硬标签" not in x["marks"] and len(x["answer"]) > 6]
        if not bad or not good:
            continue
        rejected = sorted(bad, key=lambda x: len(x["answer"]))[0]   # 取最短的坏例子
        chosen = sorted(good, key=lambda x: len(x["answer"]), reverse=True)[0]
        if rejected["answer"] == chosen["answer"]:
            continue
        pairs.append({
            "question": question,
            "chosen": chosen["answer"],
            "rejected": rejected["answer"],
            "why": f"rejected 标记：{rejected['marks']}（{rejected['file']}）",
            "chosen_from": chosen["file"],
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        for pair in pairs:
            fh.write(json.dumps({
                "messages": [{"role": "user", "content": pair["question"]}],
                "chosen": pair["chosen"],
                "rejected": pair["rejected"],
                "meta": {"why": pair["why"], "chosen_from": pair["chosen_from"]},
            }, ensure_ascii=False) + "\n")

    print(f"\n挖到 {len(pairs)} 条偏好对 → {OUT}")
    for pair in pairs[:8]:
        print(f"\n问：{pair['question']}")
        print(f"  ✓ 好：{pair['chosen'][:60]}")
        print(f"  ✗ 差：{pair['rejected'][:60]}　（{pair['why']}）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
