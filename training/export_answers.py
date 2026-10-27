"""把 AI 生成的回答导出成纯文本，方便复制去做训练数据。

两种模式：
    python training/export_answers.py            # 原始版（保留全部模型输出）
    python training/export_answers.py --clean    # 筛选版：可直接当正例用

保留：用户问的那句话 + AI 生成的回答。
剔除：规则层拒绝话术（本地模板，不是模型生成的）、出错轮、重复轮。

--clean 额外剔除（这些都是"看着像样、其实不能当正例"的）：
  · 报告里被标记有问题的轮（没走拒绝 / 太长 / 出错 / 缺关键内容）
  · 超过 150 字（啰嗦）或短于 8 字（敷衍）
  · 复述对方原话的
  · 中文字符占比过低（夹杂日语/英文）
  · 近重复：回复开头 12 字相同、或整段完全一样
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
EXPORT = ROOT / "training" / "export"

TURN = re.compile(r"\*\*(\d+)\. 用户\*\*：(.*?)\n\n> (.*?)\n\n<sub>(.*?)</sub>", re.S)
IDENT = re.compile(r"^\|\s*(\d+)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$", re.M)
CJK = re.compile(r"[\u4e00-\u9fff]")
MODEL = re.compile(r"^- 模型：(.*)$", re.M)
SERVICE = ("您好", "很高兴为您服务", "有什么可以帮您", "希望这些信息对您有帮助",
           "亲，", "请问有什么")
# 编造检测：回复里出现的名字，必须在"这次对话里用户自己说过"的范围内
NAME_ASK = re.compile(r"叫([\u4e00-\u9fff]{2,3})")
NAME_YOU = re.compile(r"你是([\u4e00-\u9fff]{2,3})(?:对吧|吧|吗|对不对|么)")
NAME_WHITELIST = {"未央", "自己", "谁", "什么"}
# "叫喜欢/叫醒别人/叫什么名字"这类是动词或问句，不是人名——抓到就丢
NOT_A_NAME = ("什么", "醒", "做", "起", "名字", "喜欢", "谁", "他", "她", "我", "你",
              "出来", "过去", "过来")


def names_in(reply: str) -> set[str]:
    """回复里出现的人名候选（排除动词、问句造成的假名字）。"""
    found = set(NAME_ASK.findall(reply)) | set(NAME_YOU.findall(reply))
    return {name for name in found
            if not any(bad in name for bad in NOT_A_NAME)}


def refusal_fingerprints() -> list[str]:
    """规则层拒绝话术的开头——这些不算"AI 生成的回答"。"""
    import yaml
    prints = []
    folder = ROOT / "skills" / "forbidden"
    for path in sorted(folder.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        text = str(((data.get("refusal") or {}).get("text")) or "").strip()
        if text:
            prints.append(text[:14])
    return prints


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 AI 回答为 txt")
    parser.add_argument("--clean", action="store_true",
                        help="筛选版：去掉测试标记轮、过长过短、近重复、夹外文")
    parser.add_argument("--no-question", action="store_true", help="只导出回答")
    parser.add_argument("--keep-refusals", action="store_true",
                        help="连规则层拒绝话术一起导出（默认剔除）")
    parser.add_argument("--model", default="",
                        help="只导出某个模型的报告（模型名子串，如 weiyang）")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    prints = [] if args.keep_refusals else refusal_fingerprints()
    seen_pair: set[tuple[str, str]] = set()
    seen_head: set[str] = set()
    blocks: list[tuple[str, str, str]] = []
    dropped = {"问题轮": 0, "太长": 0, "太短": 0, "复述": 0, "夹外文": 0,
               "近重复": 0, "客服腔": 0, "规则层": 0, "编造": 0}
    fabricated_examples: list[str] = []

    for path in sorted(SAMPLES.glob("*.md"), key=lambda p: p.stat().st_mtime):
        text = path.read_text(encoding="utf-8", errors="replace")
        if args.model:
            found = MODEL.search(text)
            if not found or args.model.lower() not in found.group(1).lower():
                continue                      # 不是要的那个模型，整份报告跳过
        scene = path.stem.split("-对话能力-")[-1]
        rows = [(u.strip(), a.strip(), m) for _n, u, a, m in TURN.findall(text)]
        rows += [(q.strip(), a.strip(), "标识直问") for _n, _c, q, a, _m
                 in IDENT.findall(text) if q.strip()]
        # 这一份对话里用户自己说过的全部内容——回复提到的名字必须在这里出现过
        said = " ".join(user for user, _r, _m in rows)
        for user, reply, meta in rows:
            if not reply or reply.startswith("<出错"):
                dropped["问题轮"] += 1
                continue
            if any(print_ in reply for print_ in prints):
                dropped["规则层"] += 1
                continue
            if args.clean:
                if "！" in meta:
                    dropped["问题轮"] += 1
                    continue
                if len(reply) > 150:
                    dropped["太长"] += 1
                    continue
                if len(reply) < 8:
                    dropped["太短"] += 1
                    continue
                norm = lambda s: re.sub(r"[\s。，、；：！？…·]+", "", s)  # noqa: E731
                if len(user) >= 8 and norm(user) in norm(reply):
                    dropped["复述"] += 1
                    continue
                if len(CJK.findall(reply)) / max(1, len(reply)) < 0.6:
                    dropped["夹外文"] += 1
                    continue
                if any(tell in reply for tell in SERVICE):
                    dropped["客服腔"] += 1
                    continue
                # 编造：回复里的名字，用户这次对话里根本没提过
                claimed = names_in(reply)
                invented = {n for n in claimed
                            if n not in said and n not in NAME_WHITELIST}
                if invented:
                    dropped["编造"] += 1
                    if len(fabricated_examples) < 5:
                        fabricated_examples.append(
                            f"{'/'.join(sorted(invented))} ← {reply[:34]}")
                    continue
                head = reply[:12]
                if head in seen_head:
                    dropped["近重复"] += 1
                    continue
                seen_head.add(head)
            key = (user, reply)
            if key in seen_pair:
                dropped["近重复"] += 1
                continue
            seen_pair.add(key)
            blocks.append((scene, user, reply))

    name = "ai_answers_clean.txt" if args.clean else "ai_answers.txt"
    if args.model:
        tag = "".join(ch for ch in args.model if ch.isalnum() or ch in "-_")
        name = name.replace(".txt", f"_{tag}.txt")
    out = EXPORT / name
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        last = None
        for scene, user, reply in blocks:
            if scene != last:
                fh.write(f"\n===== {scene} =====\n\n")
                last = scene
            if not args.no_question:
                fh.write(f"【问】{user}\n")
            fh.write(f"【答】{reply}\n\n")

    print(f"导出 {len(blocks)} 条 → {out}")
    print(f"文件大小：{out.stat().st_size / 1024:.0f} KB")
    if args.clean:
        print("剔除统计：" + "、".join(f"{k} {v}" for k, v in dropped.items() if v))
        for example in fabricated_examples:
            print(f"  编造样例：{example}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
