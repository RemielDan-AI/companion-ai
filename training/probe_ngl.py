"""找每个模型能塞进 4GB 显存的**最大 ngl**（降序探测，第一个能起来的就是答案）。

## 为什么要这个

用户要求"尽量在 GPU 跑，不然内存可能不够"——这台机器只有 **15.2 GB 物理内存**，
一个 9B Q4 纯 CPU 要占 5.5 GB RSS，加上 2B 抽取器 + 嵌入 + Windows 就快见底了。
把尽量多层卸载到 GPU（4 GB 显存）能显著降 RAM。

`ngl` 设太大会 OOM 直接起不来，所以**实测**：从高到低试，第一个健康的留下。

⚠ 判断"健康"必须**真的发一次生成**，不能只看 `/health`──
   llama.cpp 在显存不够时有时会起来但一推理就崩。

用法：
    python training/probe_ngl.py
    python training/probe_ngl.py --models Qwen3.5-9B
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CANDIDATES = [32, 28, 24, 20, 16, 12]
MODELS = ["gemma-4-E4B", "Qwen3.5-9B", "Qwen3-8B"]


def _llama() -> pathlib.Path:
    for base in (pathlib.Path(r"<你的目录>\AppData\Local\Microsoft\WinGet\Packages"),
                 pathlib.Path(r"C:\ai")):
        hit = next(base.rglob("llama-server.exe"), None)
        if hit:
            return hit
    raise SystemExit("找不到 llama-server.exe")


def _health(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _generate(port: int, timeout: float = 120.0) -> bool:
    """真发一次生成——比只看 /health 可靠。"""
    body = json.dumps({
        "messages": [{"role": "user", "content": "说一个字"}],
        "max_tokens": 4, "temperature": 0.0, "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            json.loads(r.read().decode("utf-8", "replace"))
            return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=MODELS)
    ap.add_argument("--port", type=int, default=8098)
    args = ap.parse_args()

    from model_server import discover, pick

    exe = _llama()
    print(f"llama-server: {exe}")
    best: dict[str, int] = {}

    for key in args.models:
        m = pick(discover(), key)
        print(f"\n=== {key} ===\n  {m['path']}")
        ok_ngl = None
        for ngl in CANDIDATES:
            log = pathlib.Path(f"C:/ai/tmp/ngl_{key}_{ngl}.log")
            log.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                [str(exe), "-m", m["path"], "-ngl", str(ngl), "-c", "4096",
                 "--port", str(args.port), "--no-warmup"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            # 等它起（最多 90 秒）
            t0 = time.time()
            up = False
            while time.time() - t0 < 90:
                if proc.poll() is not None:
                    break                       # 进程已经退了（OOM / 参数错）
                if _health(args.port):
                    up = True
                    break
                time.sleep(2)
            good = up and _generate(args.port)
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
            time.sleep(2)
            mark = "✅ 健康" if good else ("✗ 起了但不健康" if up else "✗ 起不来")
            print(f"  ngl={ngl:<3} {mark}")
            if good:
                ok_ngl = ngl
                break

        if ok_ngl is None:
            print("  ✗ 全部失败，只能 ngl=0 纯 CPU")
            ok_ngl = 0
        best[key] = ok_ngl

    print("\n=== 结论 ===")
    for k, v in best.items():
        print(f"  {k:<16} ngl={v}")
    out = ROOT / "training" / "ngl_plan.json"
    out.write_text(json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  已写 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
