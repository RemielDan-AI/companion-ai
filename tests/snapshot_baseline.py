"""生成微调前的基线快照，便于晚点与微调版逐项对比。

用法：python tests/snapshot_baseline.py
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROW = re.compile(r"\| 达标轮数 \| (\d+)/(\d+) \|")
MODEL = re.compile(r"^- 模型：(.*)$", re.M)


def main() -> int:
    # 每个"变体/场景"取最新一份报告（按文件名里的场景名归组）
    latest: dict[str, tuple[pathlib.Path, str, tuple[int, int]]] = {}
    for path in sorted(SAMPLES.glob("*对话能力-*.md"), key=lambda p: p.stat().st_mtime):
        text = path.read_text(encoding="utf-8", errors="replace")
        hit = ROW.search(text)
        if not hit:
            continue
        scene = path.stem.split("对话能力-")[-1]
        model = (MODEL.search(text) or [None, "未知"])[1].strip()
        latest[scene] = (path, model, (int(hit.group(1)), int(hit.group(2))))

    lines = [f"# 微调前基线（{dt.datetime.now():%Y-%m-%d %H:%M}）", "",
             "说明：每项取**最新一份**报告的达标数。微调版跑完后用同一批场景对比。", "",
             "| 场景/变体 | 达标 | 模型 | 报告 |", "|---|---|---|---|"]
    for scene in sorted(latest):
        path, model, (ok, total) = latest[scene]
        lines.append(f"| {scene} | {ok}/{total} | {model} | {path.name} |")

    total_ok = sum(v[2][0] for v in latest.values())
    total_all = sum(v[2][1] for v in latest.values())
    lines += ["", f"**合计：{total_ok}/{total_all}**（{len(latest)} 个场景）", "",
              "## 怎么测微调版", "",
              "```bat",
              "REM 1) 把微调后的模型放到 C:\\ai\\models 下，用启动器起在另一个端口",
              "python launcher.py --model 你的模型关键字 --port 8082 --ctx 8192",
              "REM 2) 让测试指向它（不用改配置文件）",
              "set COMPANION_API_URL=http://127.0.0.1:8082/v1/chat/completions",
              "REM 3) 跑同一批场景；报告里会自动写上新模型名",
              "python tests\\dialogues\\run_dialogues.py --all",
              "python tests\\make_report.py --index",
              "```", ""]
    out = SAMPLES / "基线-微调前.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写入 {out}")
    print(f"共 {len(latest)} 个场景，合计 {total_ok}/{total_all}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
