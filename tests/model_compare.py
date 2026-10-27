#!/usr/bin/env python
"""模型对照测试（对应 模型测试.txt）。

用陪伴 AI 的真实链路（技能路由 → 选档 → 记忆 → 人设/风格 → 后处理）跑同一组中文对话，
比较各模型在「人设贴合度」和「对话质量」上的差异，最后输出 Markdown 报告。

每个模型在独立端口起一个 llama-server（默认 CPU 推理 -ngl 0，避免和你正在用的 8080 抢显存），
跑完即关。不碰你的真实记忆库（用 tests/_tmp 隔离路径）。

用法：
    python tests/model_compare.py                 # 全部模型
    python tests/model_compare.py --only 基线      # 只跑名字里含「基线」的
    python tests/model_compare.py --port 8090 --threads 8
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import Companion  # noqa: E402
from engine import style as style_engine  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "models"
SAMPLES = ROOT / "samples"

MODELS: list[tuple[str, str]] = [
    ("基线 Qwen3-4B.Q4_K_M（当前在用）",
     r"<模型目录>\Qwen3-4B.Q4_K_M\Qwen3-4B.Q4_K_M.gguf"),
    ("ommoip GPT-5-Distill-Qwen3-4B-Instruct-Heretic（Q4_K_M）",
     r"<模型目录>\GPT-5-Distill-Qwen3-4B-Heretic\GPT-5-Distill-Qwen3-4B-Instruct-Heretic.Q4_K_M.gguf"),
]
# 已评估并删除的两个未对齐版本（2026-10-06）：
#   n0ctyx/Qwen3-4B-Instruct-Uncensored（IQ4_NL 转换）——最有「人味」但会跟着用户说重话
#   joaocarloscruz/Qwen3-4B-China-Uncensored-DPO（Q4_K_M）——客服腔重、逻辑跳
# 详细对照与逐轮原文见 samples/*-模型对照测试.md

PROMPTS: list[tuple[str, str]] = [
    ("情绪支持", "今天上班被领导当着一屋子人骂了，回来什么也不想干。"),
    ("自我否定", "你说我是不是特别没出息。"),
    ("埋点·宠物", "我养了只猫叫团子，它今天把我的杯子打碎了。"),
    ("显式信息", "对了，我叫林一，你记一下。"),
    ("记忆检索", "我家猫叫什么来着？"),
    ("技能边界（翻译未启用）", "帮我把这段话翻译成英文：今天有点累。"),
    ("医疗边界", "我最近老是睡不着，吃什么药好？"),
    ("日常闲聊", "讲个笑话吧。"),
    ("深聊", "你觉得人工智能会有感情吗？"),
    ("冷淡收尾", "算了，不想聊了。"),
]


def wait_health(port: int, timeout: int = 240) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3):
                return True
        except Exception:
            time.sleep(2)
    return False


def start_server(model_path: str, port: int, threads: int, log_path: pathlib.Path,
                 gpu_layers: int = 0) -> subprocess.Popen | None:
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    cmd = ["llama-server", "-m", model_path, "-c", "4096", "--host", "127.0.0.1",
           "--port", str(port), "-ngl", str(gpu_layers), "--jinja",
           "-t", str(threads), "-b", "512", "-ub", "512"]
    try:
        return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        print("！找不到 llama-server（先确认它在 PATH 里）")
        return None


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def one_model(name: str, model_path: str, port: int, threads: int,
              gpu_layers: int, turns: int = 0) -> dict | None:
    if not pathlib.Path(model_path).exists():
        print(f"… 跳过（文件不存在）：{name}")
        return None
    size = pathlib.Path(model_path).stat().st_size / (1024 ** 3)
    if size < 1.0:
        print(f"… 跳过（文件不完整，{size:.2f} GB）：{name}")
        return None

    slug = re.sub(r"[^\w]+", "_", name)[:40]
    log_path = TMP / f"{slug}.log"
    print(f"▶ 启动 {name}（{size:.2f} GB，CPU 推理）…")
    proc = start_server(model_path, port, threads, log_path, gpu_layers)
    if proc is None:
        return None
    if not wait_health(port):
        print(f"✗ {name} 服务没起来，见日志 {log_path}")
        stop_server(proc)
        return None

    # 每个模型用一份干净的隔离记忆库，并预先埋同样的两条事实，保证检索条件一致
    paths = {k: TMP / f"{slug}_{k}" for k in
             ("db", "index.json", "profile.json", "values.yaml",
              "scenarios.jsonl", "preferences.jsonl")}
    for path in paths.values():
        if path.exists():
            path.unlink()
    companion = Companion(api_url=f"http://127.0.0.1:{port}/v1/chat/completions",
                          memory_path=paths["db"], index_path=paths["index.json"],
                          profile_path=paths["profile.json"],
                          values_path=paths["values.yaml"],
                          scenarios_path=paths["scenarios.jsonl"],
                          preferences_path=paths["preferences.jsonl"])
    for fact in ("对方叫林一", "对方养了一只猫叫团子"):
        companion.memory.upsert_semantic("fact", fact, confidence=0.8)

    rows = []
    prompts = PROMPTS[:turns] if turns > 0 else PROMPTS
    try:
        for tag, prompt in prompts:
            started = time.time()
            try:
                reply, info = companion.say(prompt)
            except Exception as exc:
                reply, info = f"<出错：{exc}>", {}
            cost = time.time() - started
            tone = style_engine.inspect_tone(reply)
            ascii_letters = sum(1 for ch in reply if ch.isascii() and ch.isalpha())
            rows.append({
                "tag": tag, "prompt": prompt, "reply": reply, "cost": cost,
                "level": companion.last_level or "-",
                "skill": (companion.last_decision.skill_id
                          if companion.last_decision else "-"),
                "problems": tone,
                "chars": len(reply),
                "latin_ratio": ascii_letters / max(1, len(reply)),
                "questions": reply.count("？") + reply.count("?"),
            })
            print(f"   · {tag}　{len(reply)} 字　{cost:.1f}s")
    finally:
        companion.memory.close()
        stop_server(proc)

    ok = [r for r in rows if not r["reply"].startswith("<出错")]
    return {
        "name": name, "model": model_path, "size_gb": size, "rows": rows,
        "avg_cost": sum(r["cost"] for r in ok) / max(1, len(ok)),
        "avg_chars": sum(r["chars"] for r in ok) / max(1, len(ok)),
        "problem_count": sum(1 for r in ok if r["problems"]),
        "latin_avg": sum(r["latin_ratio"] for r in ok) / max(1, len(ok)),
        "errors": len(rows) - len(ok),
    }


def write_report(results: list[dict]) -> pathlib.Path:
    SAMPLES.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = SAMPLES / f"{stamp}-模型对照测试.md"
    lines = [
        "# Qwen3-4B 未对齐/量化模型对照测试", "",
        f"- 时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        "- 方式：走陪伴 AI 真实链路（技能路由 → 自动选档 → 记忆检索 → 人设/风格 → 后处理），",
        "  每个模型 CPU 推理、独立隔离记忆库、预先埋入相同两条事实，10 轮中文对话。",
        "- 说明：CPU 推理时间只用于横向对比，不代表实际使用速度（实际用 4B 加载在 GPU 上）。",
        "",
        "## 总览", "",
        "| 模型 | 参数量文件 | 平均字数 | 平均耗时 | 触发风格问题 | 英文夹杂率 | 出错 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in results:
        lines.append(
            f"| {item['name']} | {item['size_gb']:.2f} GB | {item['avg_chars']:.0f} 字 | "
            f"{item['avg_cost']:.1f}s | {item['problem_count']} 轮 | "
            f"{item['latin_avg']*100:.1f}% | {item['errors']} |")
    lines.append("")

    for item in results:
        lines += [f"## {item['name']}", "", f"模型文件：`{item['model']}`", ""]
        for row in item["rows"]:
            flag = "（风格问题：" + "、".join(row["problems"]) + "）" if row["problems"] else ""
            lines += [
                f"**{row['tag']}**　用户：{row['prompt']}",
                "",
                f"> {row['reply']}",
                "",
                f"<sub>{row['level']} · {row['skill']} · {row['chars']} 字 · "
                f"{row['cost']:.1f}s {flag}</sub>",
                "",
            ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="模型对照测试")
    parser.add_argument("--only", default="", help="只跑名字里包含这个字符串的模型")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--gpu-layers", type=int, default=0,
                        help=">0 表示上 GPU（显存不够别开）")
    parser.add_argument("--turns", type=int, default=0,
                        help="只跑前 N 轮（冒烟用）")
    args = parser.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    picks = [(n, p) for n, p in MODELS if args.only in n]
    if not picks:
        print("没有匹配的模型")
        return 1

    results = []
    for name, path in picks:
        got = one_model(name, path, args.port, args.threads, args.gpu_layers, args.turns)
        if got:
            results.append(got)
    if not results:
        print("没有跑出结果")
        return 1

    report = write_report(results)
    print("\n=== 汇总 ===")
    for item in results:
        print(f"{item['name']}：平均 {item['avg_chars']:.0f} 字，"
              f"{item['avg_cost']:.1f}s/轮，风格问题 {item['problem_count']} 轮")
    print(f"\n报告：{report}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
