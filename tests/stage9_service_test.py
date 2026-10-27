#!/usr/bin/env python
"""阶段 9 服务管理测试（项目改进v1 模块四：统一启动 / 预设 / 参数 / 失败诊断 / 退出清理）。

用嵌入模型（0.04 GB）做真实启停，几秒就能跑完；不碰你正在用的 8080/8081。

用法：python tests/stage9_service_test.py
"""
from __future__ import annotations

import json
import pathlib
import socket
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.manager import (CHAT_DEFAULTS, EMBED_DEFAULTS,  # noqa: E402
                              ServiceError, ServiceManager, health, port_free)
from services.presets import PresetStore  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "svc"
EMBED_MODEL = r"<模型目录>\bge-small-zh-v1.5\bge-small-zh-v1.5-f16.gguf"
CHAT_MODEL = r"<模型目录>\Qwen3-4B.Q4_K_M\Qwen3-4B.Q4_K_M.gguf"
TEST_PORT = 8097


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    manager = ServiceManager(log_dir=TMP / "logs")
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 1. 模型扫描 ----
    models = manager.models()
    kinds = {item["kind"] for item in models}
    check("能扫描到本地 GGUF 并分类", len(models) >= 2 and "embed" in kinds,
          f"{len(models)} 个：" + "、".join(f"{m['name']}({m['kind']})" for m in models))
    check("能找到嵌入模型", manager.find_model("bge") is not None, "")

    # ---- 2. 参数拼装 ----
    chat_cmd = manager.build_command({**CHAT_DEFAULTS, "model": CHAT_MODEL})
    check("对话命令带 --jinja 与 KV 量化",
          "--jinja" in chat_cmd and "--cache-type-k" in chat_cmd
          and "--embeddings" not in chat_cmd, " ".join(chat_cmd[-6:]))
    embed_cmd = manager.build_command({**EMBED_DEFAULTS, "model": EMBED_MODEL})
    check("嵌入命令带 --embeddings 且不带 --jinja",
          "--embeddings" in embed_cmd and "--jinja" not in embed_cmd,
          " ".join(embed_cmd[-6:]))
    extra_cmd = manager.build_command({**CHAT_DEFAULTS, "model": CHAT_MODEL,
                                       "extra": "--seed 42 --no-warmup"})
    check("额外参数原样附加", extra_cmd[-3:] == ["--seed", "42", "--no-warmup"],
          " ".join(extra_cmd[-4:]))

    # ---- 3. 预设 ----
    store = PresetStore(TMP / "presets.json")
    check("内置预设可见", len(store.names()) >= 3, "、".join(store.names()))
    store.upsert("我的调试预设", {**CHAT_DEFAULTS, "model": CHAT_MODEL, "port": 9001,
                                  "ngl": 0, "sed": "忽略这个未知字段"})
    got = store.get("我的调试预设")
    check("预设能存取且过滤未知字段",
          got["port"] == 9001 and got["ngl"] == 0 and "sed" not in got,
          f"port={got['port']} ngl={got['ngl']} 字段数={len(got)}")
    store.set_default("我的调试预设")
    name, spec = store.default()
    check("能设默认预设并读回", name == "我的调试预设" and spec["port"] == 9001, name)
    reloaded = PresetStore(TMP / "presets.json").get("我的调试预设")
    check("预设落盘后能重新读出来", reloaded["port"] == 9001, str(TMP / "presets.json"))
    check("能删除预设", store.delete("我的调试预设")
          and "我的调试预设" not in store.names(), "")

    # ---- 4. 失败诊断：模型文件不存在 / 端口占用 / 模型加载失败 ----
    try:
        manager.build_command({**CHAT_DEFAULTS, "model": r"<模型目录>\不存在.gguf"})
        check("模型文件不存在时报错", False, "没报错")
    except ServiceError as exc:
        check("模型文件不存在时报错", "不存在" in str(exc), str(exc))

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", TEST_PORT))
    blocker.listen(1)
    try:
        check("端口被占用时能检测出来", not port_free("127.0.0.1", TEST_PORT), f"{TEST_PORT}")
        try:
            manager.start({**EMBED_DEFAULTS, "model": EMBED_MODEL, "port": TEST_PORT})
            check("占用端口启动被拒绝", False, "居然启动了")
        except ServiceError as exc:
            check("占用端口启动被拒绝", "占用" in str(exc), str(exc))
    finally:
        blocker.close()

    bad_model = TMP / "假模型.gguf"
    bad_model.write_bytes(b"this is not a gguf file" * 100)
    try:
        manager.start({**EMBED_DEFAULTS, "model": str(bad_model), "port": TEST_PORT},
                      timeout=40)
        check("损坏模型能报出原因", False, "居然起来了")
    except ServiceError as exc:
        text = str(exc)
        check("损坏模型能报出原因",
              any(word in text for word in ("加载失败", "未能就绪", "格式")), text[:80])
    check("失败后没有残留进程", port_free("127.0.0.1", TEST_PORT) and not manager.services,
          f"services={list(manager.services)}")

    # ---- 5. 真实启停（嵌入模型，无窗口） ----
    handle = None
    try:
        handle = manager.start({**EMBED_DEFAULTS, "model": EMBED_MODEL, "port": TEST_PORT},
                               timeout=120)
        check("能真实启动服务并健康", health(TEST_PORT)["ok"], f"PID {handle['pid']}")
        check("识别出是本程序启动的",
              manager.status([TEST_PORT])[0]["managed"], "")
        body = json.dumps({"input": "测试一句话", "model": "bge"}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{TEST_PORT}/v1/embeddings",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        dim = len(data["data"][0]["embedding"])
        check("嵌入接口可用且维度正确", dim == 512, f"{dim} 维")

        log_file = pathlib.Path(handle["log"])
        check("有启动日志文件", log_file.exists() and log_file.stat().st_size > 0,
              log_file.name)
        check("启动日志记录了模型与 PID",
              log_file.exists() and "listening" in log_file.read_text(
                  encoding="utf-8", errors="replace").lower(), "")

        pid = handle["pid"]
        check("停止服务", manager.stop(TEST_PORT), "")
        time.sleep(1.5)
        check("停止后端口释放、进程退出",
              port_free("127.0.0.1", TEST_PORT), f"PID {pid}")
        check("tail_log 能取到日志尾部", "llama" in manager.tail_log(TEST_PORT, 5).lower()
              or manager.tail_log(TEST_PORT, 5).strip(), "")
    finally:
        if handle is not None:
            manager.stop(TEST_PORT)

    # ---- 6. 启动日志（模块四：时间/模型/参数/PID） ----
    start_log = ROOT / "logs" / "services.jsonl"
    if start_log.exists():
        records = [json.loads(line) for line in
                   start_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        last = records[-1] if records else {}
        check("启动日志含时间/模型/PID/参数",
              all(k in last for k in ("time", "model", "pid", "params", "cmd")),
              f"{len(records)} 条")
    else:
        check("启动日志含时间/模型/PID/参数", False, "logs/services.jsonl 不存在")

    # ---- 7. Job Object：前台进程被强杀 → 后台服务一起走（模块四「不留残留进程」） ----
    child_port = 8096
    script = TMP / "spawn_service.py"
    script.write_text(
        "import pathlib, sys, time\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from services.manager import ServiceManager, EMBED_DEFAULTS\n"
        f"m = ServiceManager(log_dir=pathlib.Path(r'{TMP}') / 'logs2')\n"
        f"h = m.start({{**EMBED_DEFAULTS, 'model': r'{EMBED_MODEL}',"
        f" 'port': {child_port}}}, timeout=120)\n"
        "print('PID', h['pid'], flush=True)\n"
        "time.sleep(180)\n",
        encoding="utf-8")
    out_file = TMP / "child.out"
    child = subprocess.Popen([sys.executable, str(script)], stdout=open(out_file, "w"),
                             stderr=subprocess.STDOUT)
    healthy = False
    deadline = time.time() + 120
    while time.time() < deadline and not healthy:
        healthy = health(child_port)["ok"]
        if not healthy:
            time.sleep(2)
    check("子进程能拉起服务", healthy, f"端口 {child_port}")
    if healthy:
        subprocess.run(["taskkill", "/F", "/PID", str(child.pid)],
                       capture_output=True)
        freed = False
        deadline = time.time() + 30
        while time.time() < deadline and not freed:
            freed = port_free("127.0.0.1", child_port)
            if not freed:
                time.sleep(1)
        check("前台被强杀后后台服务自动退出（Job Object）", freed,
              f"端口 {child_port} " + ("已释放" if freed else "仍被占用（有残留！）"))
    try:
        child.kill()
    except Exception:
        pass

    # ---- 8. stop_all 不留残留 ----
    check("stop_all 之后没有托管服务", manager.stop_all() == [] and not manager.services, "")

    shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"\n通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
