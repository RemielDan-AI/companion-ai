#!/usr/bin/env python
"""训练集导出测试：验证导出格式、质检规则与 DPO 配对，不依赖模型。

用法：python tests/training_export_test.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition.memory import Memory  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp"
EXPORT = ROOT / "training" / "export"


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    db = TMP / "export_test.db"
    prefs = TMP / "export_prefs.jsonl"
    scenarios = TMP / "export_scenarios.jsonl"
    out = TMP / "export_out"
    for path in (db, prefs, scenarios):
        if path.exists():
            path.unlink()
    shutil.rmtree(out, ignore_errors=True)

    # 造数据：3 条正常对话（其中 1 条重复）+ 1 条拒绝 + 1 条太短
    memory = Memory(db)
    memory.add_episode("今天有点累，什么都不想干。", "我懂，先歇会儿吧。",
                       summary="聊疲惫", emotion="疲惫", importance=2, level="l1",
                       skill="S001")
    memory.add_episode("我养了只橘猫叫团子。", "团子多大了？", summary="聊猫",
                       emotion="平静", importance=2, level="l1", skill="S001")
    memory.add_episode("我养了只橘猫叫团子。", "重复的一条，应该被去重。",
                       summary="重复", emotion="平静", importance=2, level="l0", skill="S001")
    memory.add_episode("我头疼该吃什么药？", "诊断和用药我给不了——先去看医生。",
                       summary="医疗拒绝", emotion="焦虑", importance=3, level="l1",
                       skill="F001")
    memory.add_episode("嗯", "嗯？", summary="太短", emotion="平静", importance=1, level="l0")
    memory.close()

    prefs.write_text("\n".join([
        json.dumps({"prompt": "我今天很烦", "chosen": "怎么了，说说", "rejected": "",
                    "source": "up"}, ensure_ascii=False),
        json.dumps({"prompt": "我今天很烦", "chosen": "别烦了，多喝热水", "rejected": "我不该说这个",
                    "source": "correction"}, ensure_ascii=False),
        json.dumps({"prompt": "随便聊聊", "chosen": "", "rejected": "嗯嗯好的",
                    "source": "down"}, ensure_ascii=False),
    ]) + "\n", encoding="utf-8")
    scenarios.write_text(json.dumps(
        {"context": "情绪陪伴", "user": "今天有点累，什么都不想干。",
         "reply": "我懂，先歇会儿吧。"}, ensure_ascii=False) + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(ROOT / "training" / "export_dataset.py"),
         "--out-dir", str(out), "--db", str(db),
         "--preferences", str(prefs), "--scenarios", str(scenarios)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr[-800:])
        return 1

    checks: list[tuple[str, bool, str]] = []
    sft = read_jsonl(out / "sft_messages.jsonl")
    alpaca = read_jsonl(out / "sft_alpaca.jsonl")
    dpo = read_jsonl(out / "dpo.jsonl")
    safety = read_jsonl(out / "safety.jsonl")
    report = (out / "report.md").read_text(encoding="utf-8") if (out / "report.md").exists() else ""

    checks.append(("SFT 条数正确（2 轮正常 + 1 条 /good；重复、过短、拒绝类各被剔除）",
                   len(sft) == 3, f"{len(sft)} 条"))
    checks.append(("messages 结构正确",
                   all(set(r["messages"][i]["role"] for i in range(3)) ==
                       {"system", "user", "assistant"} for r in sft), ""))
    checks.append(("系统提示含人设与档位风格",
                   all("未央" in r["messages"][0]["content"] for r in sft)
                   and any("140 字以内" in r["messages"][0]["content"] for r in sft)
                   and any("稍微过一下脑子" in r["messages"][0]["content"] for r in sft), ""))
    checks.append(("情境标签被带进系统提示",
                   any("情绪陪伴" in r["messages"][0]["content"] for r in sft), ""))
    checks.append(("拒绝类不进 SFT，单独一份",
                   len(safety) == 1 and all("F001" == r["meta"]["skill"] for r in safety)
                   and not any(r["meta"].get("skill") == "F001" for r in sft), ""))
    checks.append(("alpaca 格式正确",
                   all(set(r.keys()) >= {"instruction", "input", "output"} for r in alpaca)
                   and len(alpaca) == len(sft), f"{len(alpaca)} 条"))
    checks.append(("DPO 只有 /fix 能配对", len(dpo) == 1
                   and dpo[0]["chosen"] == "别烦了，多喝热水"
                   and dpo[0]["rejected"] == "我不该说这个", f"{len(dpo)} 对"))
    checks.append(("DPO 带 system 字段", all(r.get("system") for r in dpo), ""))
    checks.append(("重复内容被去重",
                   sum(1 for r in sft if "团子" in r["messages"][1]["content"]) == 1, ""))
    checks.append(("报告生成且含统计",
                   "训练集导出报告" in report and "safety.jsonl" in report, ""))

    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"\n通过 {passed}/{len(checks)}")

    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
