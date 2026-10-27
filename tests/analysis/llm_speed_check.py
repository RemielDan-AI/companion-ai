"""量一个聊天模型的**生成速度**（决定难度测试跑几次）。

4GB 显存上 8B Q4（4795MB）**装不进 VRAM**，只能 CPU（或极低 ngl），
所以跑之前先量一下 tok/s——否则不知道一轮要等多久、该跑几次。

用法：
    python tests/model_server.py run --model 8B --ngl 0 \
        --script tests/analysis/llm_speed_check.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.generate import Generator                  # noqa: E402

PROMPT = "用户说：我今天有点累。\n请用一句自然、简短的中文回应，不要解释。"


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    tag = os.environ.get("EXTRACT_MODEL_TAG", url)
    gen = Generator(api_url=url)
    print(f"生成速度检查　模型：{tag}")
    print("=" * 70)

    gen.chat("你好", remember=False)                     # 预热（含 prompt 处理）
    times = []
    chars = 0
    for i in range(3):
        t0 = time.time()
        out = gen.chat(PROMPT, remember=False)
        dt = time.time() - t0
        times.append(dt)
        chars += len(out or "")
        print(f"  第 {i+1} 次：{dt:.1f}s　输出 {len(out or '')} 字　"
              f"「{(out or '')[:40]}」")
    med = sorted(times)[len(times) // 2]
    print("-" * 70)
    print(f"  中位单轮：{med:.1f}s　（约 {chars/max(1,sum(times)):.1f} 字/秒）")
    print()
    print(f"  难度探针是 17 题 × 题内若干轮。按每题约 2 轮算：")
    print(f"    HARD_RUNS=1 → 约 {17*2*med/60:.0f} 分钟")
    print(f"    HARD_RUNS=3 → 约 {17*2*3*med/60:.0f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
