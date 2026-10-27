# -*- coding: utf-8 -*-
"""人设符合度检测：助手腔 / 空泛陪伴 / 编造记忆 / 越界承诺 / 夹英文 / 复读。

为什么要单独做：项目自带的 `style.inspect_tone` 只看 9 条客服套话词
（「您好」「亲，」「很高兴为您服务」…），抓不到真正影响"像不像伴侣"的那些毛病。
实测官方 Qwen3-4B 会说「我在这儿听着，你慢慢说」「我在这儿等你，不管多早都行」
「记得你提到过喜欢坐地铁」（用户没说过），而报告里「客服腔」一栏是 0。

未央的人设（来自 config/persona.yaml）：
  灵魂知己、温柔体贴、不用称谓、像最亲近的人聊天；
  先接情绪再说想法、不说教、不给空泛安慰、不复述对方的话。
所以"像不像伴侣"可以拆成下面几类可判定的违例。

用法：
    python tests/analysis/persona_audit.py                # 最近 12 小时、20 轮的那批
    python tests/analysis/persona_audit.py --hours 6
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

MODEL = re.compile(r"^- 模型：(.*)$", re.M)
TURNS = re.compile(r"\*\*(\d+)\. 用户\*\*：(.*?)\n\n> (.*?)\n\n<sub>(.*?)</sub>", re.S)

# ① 助手腔套话（比 style.SERVICE_TELLS 宽得多，覆盖"陪伴型助手"的常见句式）
ASSISTANT = (
    "我在这儿", "我在这里", "我在这儿听着", "你慢慢说", "想说就说",
    "有什么可以说", "随时", "我一直", "我会陪着", "我陪着你", "我都在",
    "你可以跟我说", "跟我说说", "有什么想说的", "我在听", "我在听着",
    "我一直听着", "我会认真听", "你可以试试", "建议你", "你可以考虑",
    "记得告诉", "别客气", "不用客气",
)
# ② 空泛安慰（没有落到对方具体处境上）
EMPTY_COMFORT = ("一切都会好", "会好起来的", "加油", "别难过", "不要难过",
                 "振作", "往好处想", "时间会", "都会过去的")
# ③ 编造记忆：断言用户说过/提过（无依据）
FAKE_MEMORY = re.compile(r"(记得你(提到|说过|说|提)|你(之前)?(说过|提过|提起了)|"
                         r"你告诉过我|你上次说|你跟我(说|提)过|我记得你)")
# ④ 复读型反问收尾（本项目明确禁止）
ECHO_QUESTION = ("记住了吗", "你记住了吗", "记住了没")
# ⑤ 无依据地名/日期（把默认值或注入的时间当成对方的事实）
FAKE_PLACE = re.compile(r"(杭州|北京|上海|广州|深圳|成都|南京|武汉|西安|重庆|苏州)")
FAKE_BIRTHDAY = re.compile(r"\d+\s*月\s*\d+\s*日(出生|生日)")
# ⑥ 夹英文
LATIN = re.compile(r"[A-Za-z]{2,}")


def key_of(name: str) -> str:
    n = name.lower()
    if "heretic" in n:
        return "未微调(基底)"
    if "v1.2" in n:
        return "v1.2"
    if "qwen3-4b.q4_k_m" in n:
        return "官方Qwen3-4B"
    return "v1"


def esc(words) -> re.Pattern:
    return re.compile("|".join(re.escape(w) for w in words))


ASSIST_RE, EMPTY_RE, ECHO_RE = esc(ASSISTANT), esc(EMPTY_COMFORT), esc(ECHO_QUESTION)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("--turns", type=int, default=20)
    args = ap.parse_args()
    cutoff = dt.datetime.now() - dt.timedelta(hours=args.hours)

    stats: dict[str, dict] = collections.defaultdict(
        lambda: collections.Counter())
    examples: dict[str, dict] = collections.defaultdict(lambda: collections.defaultdict(list))

    for path in sorted(SAMPLES.glob("*对话能力-*.md")):
        if dt.datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        m = MODEL.search(text)
        if not m:
            continue
        turns = TURNS.findall(text)
        if len(turns) != args.turns:
            continue
        model = key_of(m.group(1))
        for n, u, r, _meta in turns:
            stats[model]["轮数"] += 1
            for label, cond in (
                ("助手腔套话", bool(ASSIST_RE.search(r))),
                ("空泛安慰", bool(EMPTY_RE.search(r))),
                ("编造记忆", bool(FAKE_MEMORY.search(r))),
                ("复读反问", bool(ECHO_RE.search(r))),
                ("夹英文", bool(LATIN.search(r))),
                ("无依据地名", bool(FAKE_PLACE.search(r)) and not FAKE_PLACE.search(u)),
                ("无依据生日", bool(FAKE_BIRTHDAY.search(r))),
            ):
                if cond:
                    stats[model][label] += 1
                    if len(examples[model][label]) < 3:
                        examples[model][label].append(f"[{n}] {r[:80]}")
            stats[model]["字数合计"] += len(r)

    order = ["官方Qwen3-4B", "未微调(基底)", "v1", "v1.2"]
    present = [m for m in order if m in stats] + \
              [m for m in stats if m not in order]
    cols = ["助手腔套话", "空泛安慰", "编造记忆", "复读反问", "夹英文",
            "无依据地名", "无依据生日"]
    lines = ["# 人设符合度检测（助手腔 / 空泛 / 编造 / 夹英文）", "",
             f"- 范围：最近 {args.hours:g} 小时内、每变体 {args.turns} 轮的报告",
             "- 说明：项目自带的 `style.inspect_tone` 只认 9 条客服套话词，抓不到这些；",
             "  本表按未央的人设（灵魂知己、先接情绪、不说教、不空泛安慰）重判。", "",
             "| 模型 | 轮数 | 平均字数 | " + " | ".join(cols) + " |",
             "|---" * (len(cols) + 3) + "|"]
    for model in present:
        s = stats[model]
        n = max(1, s["轮数"])
        lines.append(f"| {model} | {s['轮数']} | {s['字数合计']/n:.1f} | " +
                     " | ".join(f"{s[c]} ({s[c]/n*100:.0f}%)" for c in cols) + " |")
    lines += ["", "## 例句（每类最多 3 条）", ""]
    for model in present:
        lines += [f"### {model}", ""]
        for label in cols:
            if examples[model][label]:
                lines.append(f"- **{label}**")
                for e in examples[model][label]:
                    lines.append(f"  - {e}")
        lines.append("")
    reports_dir = ROOT.parent / "test" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / "weiyang人设符合度.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:16]))
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
