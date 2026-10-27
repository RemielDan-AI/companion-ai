# -*- coding: utf-8 -*-
"""起嵌入服务 → 跑检索标尺 → 停服务（同进程，不留残留）。

为什么要写这个：项目用 Job Object 保证「关前台即退出」，起服务的进程一退出
llama-server 就被连带清掉，所以「一条命令起服务、下一条命令测」走不通。
必须把 起→测→停 收在一次调用里。

用法：
    python tests/analysis/retrieval_bench_with_embed.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.manager import ServiceManager, EMBED_DEFAULTS, DEFAULT_MODEL_ROOTS


def find_embed_model() -> str | None:
    for root in DEFAULT_MODEL_ROOTS:
        p = pathlib.Path(root)
        if not p.exists():
            continue
        for hit in p.rglob("*bge*.gguf"):
            return str(hit)
    return None


def main() -> int:
    model = find_embed_model()
    if not model:
        print("找不到 bge 嵌入模型，无法测带向量的基线")
        return 1
    print(f"嵌入模型：{model}")

    spec = {**EMBED_DEFAULTS, "model": model, "port": 8081}
    mgr = ServiceManager()
    print("启动嵌入服务…")
    try:
        handle = mgr.start(spec, wait=True)
        print(f"启动结果：healthy={handle.get('healthy')} pid={handle.get('pid')}")
    except Exception as exc:
        print(f"启动失败：{type(exc).__name__}: {exc}")
        return 1
    try:
        from cognition.vectors import health
        print(f"健康检查：{health()}")
        print()
        rc = subprocess.call([sys.executable, str(HERE / "retrieval_bench.py")], cwd=str(ROOT))
        return rc
    finally:
        print()
        print("停止嵌入服务…")
        try:
            mgr.stop(8081)
            print("已停止")
        except Exception as exc:
            print(f"停止失败：{exc}")


if __name__ == "__main__":
    raise SystemExit(main())
