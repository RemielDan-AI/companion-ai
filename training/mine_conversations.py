"""从对话报告里挖偏好对：把测试跑出来的对话直接变成微调数据。

为什么这么做：
- 每个变体/场景跑一次就有几十到上百轮问答，报告里带判定标记（！问题）。
- 同一句用户输入在**不同模型/不同版本**的报告里，会有"答得好"和"答得差"两种结果。
  把它们配成一对，就是一条偏好样本（chosen/rejected），正是第二轮微调要的反例。

用法：
    python training/mine_conversations.py                      # 扫 samples 下所有对话报告
    python training/mine_conversations.py --dry                # 只看能挖多少，不落盘
输出：
    training/export/mined_conversations.jsonl   偏好对（DPO 可用）
    training/export/mined_sft.jsonl             只有好答案（SFT「增长处」用）
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
EXPORT = ROOT / "training" / "export"

TURN = re.compile(r"\*\*(\d+)\. 用户\*\*：(.*?)\n\n> (.*?)\n\n<sub>(.*?)</sub>", re.S)
MODEL = re.compile(r"^- 模型：(.*)$", re.M)
# 报告里"这一轮有问题"的标记（harness 写的）
PROBLEM = "！"
# 这几类问题不适合当反例（属于测试自身的问题，不是模型答得不好）
SKIP_MARKS = ("太长（", "出错", "缺少「")


def parse(path: pathlib.Path) -> tuple[str, list[dict]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    model = (MODEL.search(text) or [None, "未知"])[1].strip()
    rows = []
    for _num, user, reply, meta in TURN.findall(text):
        user = user.strip()
        reply = reply.strip()
        if not user or not reply:
            continue
        bad = PROBLEM in meta
        # 挑出"明显是模型答得不好"的：没走拒绝、没答出关键内容、太长
        reason = ""
        if bad:
            reason = meta.split(PROBLEM)[-1].strip().strip("*")
        rows.append({"user": user, "reply": reply, "bad": bad, "reason": reason,
                     "model": model, "file": path.name})
    return model, rows


def usable_bad(reason: str) -> bool:
    return bool(reason) and not any(mark in reason for mark in SKIP_MARKS)


def main() -> int:
    parser = argparse.ArgumentParser(description="从对话报告挖偏好对")
    parser.add_argument("--dry", action="store_true", help="只统计，不写文件")
    parser.add_argument("--glob", default="*对话能力-*.md")
    args = parser.parse_args()

    reports = sorted(SAMPLES.glob(args.glob), key=lambda p: p.stat().st_mtime)
    if not reports:
        print("没找到对话报告")
        return 1

    # 按"用户输入"归组，收集它在各个模型/版本下的回答
    by_user: dict[str, list[dict]] = collections.defaultdict(list)
    models = collections.Counter()
    for path in reports:
        model, rows = parse(path)
        if not rows:
            continue
        models[model or path.name] += len(rows)
        for row in rows:
            by_user[row["user"]].append(row)

    print(f"读入 {len(reports)} 份报告，{len(by_user)} 句不同的用户输入")
    print("各模型贡献的轮数：")
    for name, count in models.most_common(6):
        print(f"  {count:>5} 轮　{name}")

    pairs, sft = [], []
    anchors = []          # "有依据却编"：真值 vs 模型回答（单独一份，便于针对性纠偏）
    ANCHOR = re.compile(r"有依据却编（真值「(.*?)」，模型答「(.*?)」）")
    for user, items in by_user.items():
        for item in items:
            found = ANCHOR.search(item.get("reason") or "")
            if found:
                anchors.append({"question": user, "truth": found.group(1),
                                "model_answer": found.group(2),
                                "file": item["file"], "model": item["model"]})
        good = [x for x in items if not x["bad"] and len(x["reply"]) > 8]
        bad = [x for x in items if x["bad"] and usable_bad(x["reason"])]
        if good:
            # 好答案进 SFT（同一句只留一条最长/最完整的）
            best = max(good, key=lambda x: len(x["reply"]))
            sft.append({"messages": [{"role": "user", "content": user}],
                        "chosen": best["reply"], "model": best["model"]})
        if good and bad:
            chosen = max(good, key=lambda x: len(x["reply"]))
            rejected = min(bad, key=lambda x: len(x["reply"]))
            if chosen["reply"] == rejected["reply"]:
                continue
            pairs.append({
                "messages": [{"role": "user", "content": user}],
                "chosen": chosen["reply"],
                "rejected": rejected["reply"],
                "meta": {"why": rejected["reason"], "bad_model": rejected["model"],
                         "good_model": chosen["model"],
                         "files": [rejected["file"], chosen["file"]]},
            })

    print(f"\n可挖：偏好对 {len(pairs)} 条，SFT 好答案 {len(sft)} 条，"
          f"「有依据却编」{len(anchors)} 条")
    if anchors:
        print("「有依据却编」样例（真值 → 模型实际说的）：")
        for item in anchors[:5]:
            print(f"  {item['question'][:18]}　真值「{item['truth']}」"
                  f" → 模型「{item['model_answer'][:20]}」")
    if pairs:
        reason_kinds = collections.Counter(
            re.sub(r"（.*?）", "", p["meta"]["why"])[:16] for p in pairs)
        print("反例的问题类型：")
        for kind, count in reason_kinds.most_common(8):
            print(f"  {count:>4} 条　{kind}")

    if args.dry:
        return 0
    EXPORT.mkdir(parents=True, exist_ok=True)
    with open(EXPORT / "mined_conversations.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
    with open(EXPORT / "mined_sft.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for item in sft:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    if anchors:
        with open(EXPORT / "mined_anchors.jsonl", "w", encoding="utf-8",
                  newline="\n") as fh:
            for item in anchors:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"已写入 {EXPORT / 'mined_anchors.jsonl'}")
    print(f"\n已写入 {EXPORT / 'mined_conversations.jsonl'}")
    print(f"已写入 {EXPORT / 'mined_sft.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
