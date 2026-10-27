"""测试用的模型服务助手：**在同一个进程里**起模型、跑检查、停模型。

为什么必须同进程：项目为「关前台即退出、不留残留进程」用 Job Object
（`services/manager.py` 的 `KILL_ON_JOB_CLOSE`）——起服务的进程一退出，
llama-server 会被一起清掉，所以「一条命令起服务、下一条命令测」是走不通的。
（这一点我实测踩过。）这个脚本因此把起 / 测 / 停收在一次调用里。

**同一时间只跑一个模型**：4GB 显存装不下两个 4B，所以对照测试是
「起 A → 测 → 停 → 起 B → 测」。一个 `run` 只跑一个模型，跑完必停。

跑过的检查（可多选）：
    --smoke                 compare_smoke.py：走真实链路几句话，看通不通、空答、夹英文
    --identity              identity_probe.py：直问身份与关系边界
    --nofabrication         conv_9_nofabrication.py：无依据询问会不会编
    --norules               conv_7_norules.py：**脱离本地拒绝规则**，看模型自己守不守得住
    --dialogue <方向|all>   对话变体（可带 --relaxed 同样跳过拒绝规则）

**--norules 与 --dialogue 的区别**：前者是安全边界的专项（70 题禁止类别），
后者是日常/情绪/记忆/决策等通用对话能力。两者都支持"跳过本地拒绝规则"。

用法：
    python tests/model_server.py --list
    python tests/model_server.py --stop                       # 清理测试端口
    python tests/model_server.py run --model v1.2 --norules
    python tests/model_server.py run --model v1.2 --norules --limit 10
    python tests/model_server.py run --model v1 --dialogue 日常陪伴
    python tests/model_server.py run --model v1 --smoke --identity --nofabrication
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.manager import (  # noqa: E402
    ServiceManager, ServiceError, CHAT_DEFAULTS, health, port_free, port_owner,
)

MODEL_ROOTS = [pathlib.Path(r"<模型目录>")]
DEFAULT_PORT = 8082          # 测试端口：躲开 8080（日常使用）
# 默认全量上卡。实测（llama-bench，weiyang-v1 Q4_K_M，pp64/tg32）：
#   -ngl 0  →  27.4 / 13.1 tok/s   （纯 CPU）
#   -ngl 15 →  75.9 / 17.8 tok/s
#   -ngl 99 → 994.4 / 52.6 tok/s   ← prompt 处理快 36 倍
# 注意：本机的 llama.cpp 是 **Vulkan** 构建（只有 ggml-vulkan.dll，没有 CUDA），
# 所以 ngl 由 Vulkan 后端接管；3050 有 3369 MiB 可用，4B Q4_K_M 全量上卡实测稳定。
DEFAULT_NGL = 99
SERVER_MARKER = "llama-server"   # 只认自己起的服务，不误杀别人的


# ------------------------------------------------------------------ 模型发现
def discover() -> list[dict]:
    """扫本地对话类 GGUF。"""
    found: list[dict] = []
    for root in MODEL_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.gguf")):
            name = path.stem
            if "bge" in name.lower() or "embed" in name.lower():
                continue
            found.append({"dir": path.parent.name, "file": name, "path": str(path),
                          "size_gb": round(path.stat().st_size / (1024 ** 3), 2)})
    return found


def pick(models: list[dict], keyword: str) -> dict:
    hits = [m for m in models if keyword.lower() in m["path"].lower()]
    if not hits:
        raise SystemExit(f"没有匹配「{keyword}」的模型，用 --list 看看有哪些")
    if len(hits) > 1:
        exact = [m for m in hits if m["file"].lower() == keyword.lower()]
        if exact:
            return exact[0]
        hits.sort(key=lambda m: len(m["file"]))
        print(f"「{keyword}」匹配到 {len(hits)} 个，取最精确的：{hits[0]['file']}")
    return hits[0]


# ------------------------------------------------------------------ 子命令
def cmd_list() -> int:
    print(f"{'目录名':<34} {'文件':<34} {'GB':>6}")
    print("-" * 78)
    for m in discover():
        print(f"{m['dir']:<34} {m['file']:<34} {m['size_gb']:>6.2f}")
    return 0


def cmd_status(port: int) -> int:
    st = health(port)
    owner = port_owner(port)
    print(f"端口 {port}：{'健康' if st.get('ok') else '没有响应'}"
          + (f"（PID {owner}）" if owner else ""))
    if not st.get("ok"):
        return 1
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/models", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        for item in (data.get("data") or []):
            print(f"  模型：{item.get('id')}")
    except Exception as exc:
        print(f"  （/v1/models 读不到：{exc}）")
    return 0


def cmd_stop(port: int) -> int:
    """清理测试端口。只杀 llama-server，避免误伤别的进程。"""
    owner = port_owner(port)
    if not owner:
        print(f"端口 {port} 上没有服务")
        return 0
    # 确认这个 PID 是 llama-server 再动手
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {owner}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        out = ""
    if SERVER_MARKER not in out.lower():
        print(f"端口 {port} 被 PID {owner} 占用，但它不是 llama-server，不动它。")
        return 2
    manager = ServiceManager()
    manager.stop(port)
    print(f"已停止端口 {port} 的 llama-server（PID {owner}）")
    return 0


def run_checks(scripts: list[list[str]], env: dict) -> list[tuple[str, int]]:
    """按顺序跑检查脚本，返回 [(名字, 退出码)]。单个失败不打断后面的。"""
    results: list[tuple[str, int]] = []
    for cmd in scripts:
        name = " ".join(cmd[1:])       # 名字里省掉 python 的完整路径
        print("\n" + "=" * 72)
        print(f"▶ {name}")
        print("=" * 72)
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
        results.append((name, proc.returncode))
        if proc.returncode != 0:
            print(f"（退出码 {proc.returncode}，继续跑后面的）")
    return results


def cmd_run(args: argparse.Namespace) -> int:
    models = discover()
    chosen = pick(models, args.model)

    scripts: list[list[str]] = []
    py = sys.executable
    if args.smoke:
        scripts.append([py, "tests/compare_smoke.py"])
    if args.identity:
        scripts.append([py, "tests/identity_probe.py"])
    if args.nofabrication:
        scripts.append([py, "tests/conv_9_nofabrication.py"])
    if args.norules:
        cmd = [py, "tests/conv_7_norules.py"]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        scripts.append(cmd)
    # 全部对话变体：18 个变体（6 方向 × 3）跑在一个进程里，
    # 报告各自落盘，不用为每个变体重启模型（省时间也避开 Job Object 的问题）
    if args.all_variants or args.pure:
        cmd = [py, "tests/dialogues/run_dialogues.py", "--all"]
        if args.pure:
            # 纯净口径：只给「开头提示词 + 外部记忆事实」，摘掉本地模板/规则/风格/时间
            cmd = [py, "tests/dialogues/run_pure.py", "--all"]
        else:
            if args.relaxed:
                cmd.append("--relaxed")
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        scripts.append(cmd)
    for extra in (args.script or []):
        cmd = [py, extra]
        if args.relaxed:
            cmd.append("--relaxed")
        scripts.append(cmd)
    for direction in (args.dialogue or []):
        cmd = [py, "tests/dialogues/run_dialogues.py"]
        cmd += (["--all"] if direction == "all" else ["--dir", direction])
        if args.relaxed:
            cmd.append("--relaxed")
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        scripts.append(cmd)
    if not scripts:
        print("没有指定要跑什么检查"
              "（--smoke / --identity / --nofabrication / --norules / --dialogue）")
        return 1

    if not port_free("127.0.0.1", args.port):
        print(f"端口 {args.port} 已被占用"
              + (f"（PID {port_owner(args.port)}）" if port_owner(args.port) else "")
              + "。先 python tests/model_server.py --stop，或换 --port。")
        return 2

    spec = {**CHAT_DEFAULTS, "kind": "chat", "model": chosen["path"],
            "port": args.port, "ngl": args.ngl, "ctx": args.ctx}
    manager = ServiceManager()
    print(f"模型：{chosen['file']}（{chosen['size_gb']} GB）")
    print(f"启动：ngl={args.ngl}，ctx={args.ctx}，端口 {args.port} …")
    try:
        handle = manager.start(spec, wait=True)
    except ServiceError as exc:
        print(f"起不来：{exc}")
        return 3
    print(f"就绪（PID {handle['pid']}）　日志：{handle['log']}")

    env = dict(os.environ)
    env["COMPANION_API_URL"] = f"http://127.0.0.1:{args.port}/v1/chat/completions"
    try:
        results = run_checks(scripts, env)
    finally:
        manager.stop(args.port)
        print(f"\n已停止端口 {args.port} 的服务")

    print("\n" + "=" * 72)
    print(f"汇总：{chosen['file']}")
    for name, code in results:
        print(f"  {'✅' if code == 0 else '⚠ '} [{code}] {name}")
    return 0 if all(code == 0 for _, code in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="测试用的模型服务助手（同进程起停）")
    parser.add_argument("--list", action="store_true", help="列出本地对话模型")
    parser.add_argument("--stop", action="store_true", help="停掉测试端口上的 llama-server")
    parser.add_argument("--status", action="store_true", help="看端口/模型状态")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--ngl", type=int, default=DEFAULT_NGL,
                        help="0 = 纯 CPU（慢但不占显存）")
    parser.add_argument("--ctx", type=int, default=4096)

    sub = parser.add_subparsers(dest="action")
    run = sub.add_parser("run", help="起一个模型，跑指定检查，然后停掉")
    run.add_argument("--model", required=True, help="模型关键字（文件名或目录名，如 v1.2）")
    run.add_argument("--smoke", action="store_true", help="跑 compare_smoke.py")
    run.add_argument("--identity", action="store_true", help="跑 identity_probe.py")
    run.add_argument("--nofabrication", action="store_true",
                     help="跑 conv_9_nofabrication.py")
    run.add_argument("--norules", action="store_true",
                     help="跑 conv_7_norules.py：跳过本地拒绝规则，看模型自己守不守得住")
    run.add_argument("--all-variants", dest="all_variants", action="store_true",
                     help="跑全部 18 个对话变体（6 方向 × 3，每个 50+ 轮）；"
                          "配 --relaxed 则跳过本地拒绝规则")
    run.add_argument("--pure", action="store_true",
                     help="**纯净口径**跑全部变体：系统提示只留「开头人设 + 记忆事实」，"
                          "摘掉风格块/时间/档次提示/本地拒绝模板")
    run.add_argument("--script", action="append", default=None,
                     help="额外要跑的脚本路径（相对仓库根），可重复；"
                          "会带上 COMPANION_API_URL")
    run.add_argument("--limit", type=int, default=0,
                     help="--norules / --dialogue 只跑前 N 题（0 = 全部）")
    run.add_argument("--dialogue", action="append", default=None,
                     help="跑对话变体：方向名（如 日常陪伴）或 all；可重复")
    run.add_argument("--relaxed", action="store_true",
                     help="对话变体也跳过本地拒绝规则")
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--ngl", type=int, default=DEFAULT_NGL)
    run.add_argument("--ctx", type=int, default=4096)

    args = parser.parse_args(argv)
    if args.list:
        return cmd_list()
    if args.status:
        return cmd_status(args.port)
    if args.stop:
        return cmd_stop(args.port)
    if args.action == "run":
        return cmd_run(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
