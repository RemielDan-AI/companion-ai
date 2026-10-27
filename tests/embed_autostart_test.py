"""钉住「启动时自动拉起嵌入服务」的行为。

## 最重要的那条

**构造 `Companion` 绝不能拉起嵌入服务**。测试会到处构造 Companion，
一旦自动拉起写进类里，跑一次回归就会起一堆 llama-server，
还会和自带起停嵌入服务的基准测试（`retrieval_bench_with_embed.py`）打架。
所以自动拉起只允许出现在**进程入口**（gui/app/pet_server 的 `main()`）。

本测试**不连真模型、不起服务**：用「关掉开关」「占住端口」「静态查位置」三种手段。
"""
from __future__ import annotations

import os
import pathlib
import socket
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 72)
    print("① 位置：自动拉起只允许在进程入口，**不能**在 Companion 类里")
    print("=" * 72)
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    cut = app_src.find("def main()")
    check(cut > 0, "app.py 里能找到 main()")
    check("ensure_embed_service" not in app_src[:cut],
          "Companion 类定义段里**没有**自动拉起",
          "（有的话测试会到处起 llama-server）")
    check("ensure_embed_service" in app_src[cut:], "app.py 的 main() 里有自动拉起")

    for name in ("gui.py", "pet_server.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        check("ensure_embed_service" in src, f"{name} 里有自动拉起")

    print()
    print("=" * 72)
    print("② 开关：COMPANION_NO_EMBED_AUTOSTART=1 时必须直接跳过（不起服务）")
    print("=" * 72)
    os.environ["COMPANION_NO_EMBED_AUTOSTART"] = "1"
    from services.autostart import ensure_embed_service
    try:
        res = ensure_embed_service(verbose=False)
        check(res["action"] == "disabled", "返回 action=disabled",
              f"实际 {res['action']!r}")
        check(res["pid"] is None, "没有起进程")
    finally:
        os.environ.pop("COMPANION_NO_EMBED_AUTOSTART", None)

    print()
    print("=" * 72)
    print("③ 不抢占：端口被占但不是嵌入服务时，必须报 foreign 而不动手")
    print("=" * 72)
    # 占一个空闲端口，假装别人在用
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    busy_port = probe.getsockname()[1]
    probe.listen(1)
    try:
        res = ensure_embed_service(port=busy_port, verbose=False, wait=False)
        check(res["action"] in ("foreign", "already"),
              "端口被占时不启动新进程", f"实际 action={res['action']!r}")
        check(res["action"] == "foreign", "判定为 foreign（不是我们的服务）")
    finally:
        probe.close()

    print()
    print("=" * 72)
    print("④ 探测函数本身要稳：没人听的时候返回 False 而不是抛异常")
    print("=" * 72)
    from services.autostart import _embeddings_alive
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    try:
        check(_embeddings_alive("127.0.0.1", dead_port, timeout=2) is False,
              "空端口返回 False，不抛异常")
    except Exception as exc:
        check(False, "空端口应返回 False", f"抛了 {type(exc).__name__}")

    print()
    print("=" * 72)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
