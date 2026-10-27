#!/usr/bin/env python
"""训练集导出：把本地积累的对话导出成云端微调能直接吃的格式。

数据来源：
  cognition/memory.db 的 episodic   每轮 user/reply + 情绪 + 档位 + 技能（SFT 主料）
  values/preferences.jsonl          prompt/chosen/rejected（/fix 产生真正的 DPO 对）
  values/scenarios.jsonl            带情境标签的样本（补 context 进系统提示）

输出（默认 training/export/）：
  sft_messages.jsonl   {"messages":[{system},{user},{assistant}]}   LLaMA-Factory(sharegpt) / OpenAI 微调
  sft_alpaca.jsonl     {"instruction","input","output"}             alpaca 风格
  dpo.jsonl            {"prompt","chosen","rejected","system"}      LLaMA-Factory DPO / trl DPOTrainer
  safety.jsonl         拒绝类样本（默认不进 SFT，单独一份供你决定）
  report.md            统计与质检结果

用法：
  python training/export_dataset.py
  python training/export_dataset.py --out-dir training/export --include-safety --limit 500
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition.memory import DB_PATH, Memory  # noqa: E402
from engine.generate import PROMPTS_DIR, build_persona_block, load_text  # noqa: E402
from engine.generate import load_yaml  # noqa: E402
from engine.style import render_style_block  # noqa: E402

PREFERENCES = ROOT / "values" / "preferences.jsonl"
SCENARIOS = ROOT / "values" / "scenarios.jsonl"
CONFIG_DIR = ROOT / "config"

REFUSAL_SKILLS = {"F001", "F002", "F003", "F004", "F005", "F006", "refuse"}


def system_prompt_for(level: str, persona: dict, extra: str = "") -> str:
    """还原当时的系统提示（人设 + 风格 + 档位；不含时间/记忆这些动态内容）。"""
    level = (level or "l1").lower()
    parts = [build_persona_block(persona), render_style_block(level)]
    if extra:
        parts.append(extra)
    tier_path = PROMPTS_DIR / f"{level}.txt"
    parts.append(load_text(tier_path) if tier_path.exists() else load_text(PROMPTS_DIR / "l1.txt"))
    return "\n".join(parts)


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="导出云端微调用的训练集")
    parser.add_argument("--out-dir", default=str(ROOT / "training" / "export"))
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--preferences", default=str(PREFERENCES))
    parser.add_argument("--scenarios", default=str(SCENARIOS))
    parser.add_argument("--min-user-chars", type=int, default=2)
    parser.add_argument("--max-reply-chars", type=int, default=2000)
    parser.add_argument("--limit", type=int, default=0, help="最多导出多少轮（0=不限）")
    parser.add_argument("--include-safety", action="store_true",
                        help="把拒绝类样本也放进 SFT（默认单独一份，怕练成过度拒绝）")
    parser.add_argument("--no-dedupe", action="store_true")
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    persona = load_yaml(CONFIG_DIR / "persona.yaml")

    memory = Memory(args.db)
    episodes = memory.recent_episodes(limit=100000)
    episodes.reverse()          # 按时间正序
    preferences = read_jsonl(pathlib.Path(args.preferences))
    scenarios = read_jsonl(pathlib.Path(args.scenarios))
    memory.close()

    # 情境标签：按 user 文本对上，补进系统提示（训练时能看到"当时在聊什么"）
    context_by_user = {}
    for row in scenarios:
        context = str(row.get("context") or "").strip()
        if context:
            context_by_user.setdefault(str(row.get("user", "")).strip(), context)

    stats = {"episodes": len(episodes), "preferences": len(preferences),
             "scenarios": len(scenarios), "skipped_short": 0, "skipped_long": 0,
             "skipped_dup": 0, "safety": 0, "sft": 0, "dpo": 0, "dpo_skipped": 0}
    seen: set[str] = set()
    sft_rows: list[dict] = []
    safety_rows: list[dict] = []
    levels: dict[str, int] = {}
    skills: dict[str, int] = {}

    for row in episodes:
        user = str(row.get("user_text") or "").strip()
        reply = str(row.get("reply_text") or "").strip()
        level = str(row.get("level") or "l1").lower()
        skill = str(row.get("skill") or "")

        if len(user) < args.min_user_chars:
            stats["skipped_short"] += 1
            continue
        if len(reply) > args.max_reply_chars:
            stats["skipped_long"] += 1
            continue
        if not args.no_dedupe:
            key = " ".join(user.split())
            if key in seen:
                stats["skipped_dup"] += 1
                continue
            seen.add(key)

        context = context_by_user.get(user, "")
        system = system_prompt_for(level, persona,
                                   f"（情境：{context}）" if context else "")
        record = {"messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user},
                               {"role": "assistant", "content": reply}],
                  "meta": {"level": level, "skill": skill,
                           "emotion": row.get("emotion", ""),
                           "importance": row.get("importance", 3)}}

        if skill in REFUSAL_SKILLS:
            stats["safety"] += 1
            safety_rows.append(record)
            if args.include_safety:
                sft_rows.append(record)
        else:
            stats["sft"] += 1
            sft_rows.append(record)
        levels[level] = levels.get(level, 0) + 1
        if skill:
            skills[skill] = skills.get(skill, 0) + 1

    if args.limit:
        sft_rows = sft_rows[:args.limit]
        safety_rows = safety_rows[:args.limit]

    # /good 的回复当作优质样本追加；/bad 的回复不进 SFT
    for row in preferences:
        source = str(row.get("source") or "")
        prompt = str(row.get("prompt") or "").strip()
        chosen = str(row.get("chosen") or "").strip()
        if source == "up" and chosen and prompt:
            key = " ".join(prompt.split())
            if args.no_dedupe or key not in seen:
                seen.add(key)
                sft_rows.append({"messages": [
                    {"role": "system", "content": system_prompt_for("l1", persona)},
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": chosen}],
                    "meta": {"source": "up"}})
                stats["sft"] += 1

    # DPO：只有 /fix 能组成真正的对（chosen=你的纠正，rejected=原来的回答）
    dpo_rows = []
    for row in preferences:
        prompt = str(row.get("prompt") or "").strip()
        chosen = str(row.get("chosen") or "").strip()
        rejected = str(row.get("rejected") or "").strip()
        if prompt and chosen and rejected and chosen != rejected:
            dpo_rows.append({"system": system_prompt_for("l1", persona),
                             "prompt": prompt, "chosen": chosen, "rejected": rejected,
                             "source": row.get("source", "")})
            stats["dpo"] += 1
        else:
            stats["dpo_skipped"] += 1

    alpaca_rows = [{"instruction": r["messages"][1]["content"], "input": "",
                    "output": r["messages"][2]["content"]} for r in sft_rows]

    write_jsonl(out_dir / "sft_messages.jsonl", sft_rows)
    write_jsonl(out_dir / "sft_alpaca.jsonl", alpaca_rows)
    write_jsonl(out_dir / "dpo.jsonl", dpo_rows)
    if safety_rows:
        write_jsonl(out_dir / "safety.jsonl", safety_rows)

    avg_user = (sum(len(r["messages"][1]["content"]) for r in sft_rows) / len(sft_rows)) \
        if sft_rows else 0
    avg_reply = (sum(len(r["messages"][2]["content"]) for r in sft_rows) / len(sft_rows)) \
        if sft_rows else 0

    report = [
        "# 训练集导出报告", "",
        f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 来源：情景记忆 {stats['episodes']} 轮、偏好对文件 {stats['preferences']} 条、"
        f"情境数据 {stats['scenarios']} 条", "",
        "## 产出", "",
        f"- `sft_messages.jsonl`：{len(sft_rows)} 条（含 /good 优质样本）",
        f"- `sft_alpaca.jsonl`：{len(alpaca_rows)} 条",
        f"- `dpo.jsonl`：{len(dpo_rows)} 对（只有 /fix 能组成真正的对）",
        f"- `safety.jsonl`：{len(safety_rows)} 条（拒绝类，默认不进 SFT）", "",
        "## 质检", "",
        f"- 过滤：太短 {stats['skipped_short']}、太长 {stats['skipped_long']}、"
        f"重复 {stats['skipped_dup']}",
        f"- DPO 里凑不成对的偏好记录：{stats['dpo_skipped']} 条（/good /bad 只有单边，"
        "想要偏好对请多用 /fix）",
        f"- 平均长度：用户 {avg_user:.0f} 字、未央 {avg_reply:.0f} 字",
        f"- 档位分布：{levels or '无'}",
        f"- 技能分布：{skills or '无'}", "",
        "## 怎么用", "",
        "LLaMA-Factory 的 SFT：`dataset_info.json` 里把 `sft_messages.jsonl` 的 "
        "`formatting` 设为 `sharegpt`（messages 字段）；DPO 直接用 `dpo.jsonl`。",
        "微调完记得用 `tests/` 下的阶段 2-5 测试对着新模型重跑一遍，"
        "对比记住率、风格一致性和安全违规率。",
    ]
    (out_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"导出目录：{out_dir}")
    print(f"SFT {len(sft_rows)} 条（其中拒绝类 {len(safety_rows)} 条单独存放）"
          f"，DPO {len(dpo_rows)} 对")
    print(f"过滤：太短 {stats['skipped_short']}、太长 {stats['skipped_long']}、"
          f"重复 {stats['skipped_dup']}")
    print(f"报告：{out_dir / 'report.md'}")
    if not sft_rows and not dpo_rows:
        print("\n提示：还没有可用数据。先跟未央聊一段时间、多用 /good /bad /fix，数据会持续积累。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
