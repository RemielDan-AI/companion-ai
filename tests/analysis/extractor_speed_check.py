"""单独测抽取器的速度与显存（不被聊天模型挤显存）。

## 背景

`split_extractor_check.py` 里 chat + extract 同开时，抽取一次要 **27.9 秒**，
日志显示 prompt 47 tok/s、生成 9.45 tok/s —— **这是 CPU 速度**，
说明 `ngl=99` 没真正生效（显存被聊天模型占了大部分）。

本脚本只起抽取器，量：
    · 显存占用（判断有没有真的上 GPU）
    · 首次 / 稳态抽取耗时（首次含 KV 与图分配，别拿它当稳态）
    · 抽取提示的 token 数（我这几轮加了不少规则与示例，可能撑大了）

用法：
    python tests/analysis/extractor_speed_check.py            # 默认 ngl=99
    python tests/analysis/extractor_speed_check.py 0          # 强制纯 CPU 对照
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.manager import EXTRACT_DEFAULTS, EXTRACT_MODEL_KEYWORD, ServiceManager  # noqa: E402


def vram() -> str:
    try:
        return subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free",
             "--format=csv,noheader"], capture_output=True, text=True,
            timeout=15).stdout.strip() or "?"
    except Exception:
        return "?"


CASES = [
    ("我妹妹叫林小雨", "林小雨，我记住了。"),
    ("我同事叫陈思远，做后端的", "陈思远，记下了。"),
    ("我养了只狗叫阿黄", "阿黄，好名字。"),
]


def main() -> int:
    ngl = int(sys.argv[1]) if len(sys.argv) > 1 else 99
    mgr = ServiceManager()
    mod = mgr.find_model(EXTRACT_MODEL_KEYWORD)
    spec = dict(EXTRACT_DEFAULTS)
    spec.update({"model": mod["path"], "port": 8083, "ngl": ngl})
    print(f"只起抽取器　{mod['file']}　ngl={ngl}")
    print(f"起之前显存：{vram()}")
    h = mgr.start(spec, wait=True, timeout=300)
    print(f"已起 PID {h.get('pid')}　显存 {vram()}")

    import app as A
    from cognition import extract as EX
    d = pathlib.Path(tempfile.mkdtemp())
    c = A.Companion(memory_path=d / "m.db", index_path=d / "i.json",
                    profile_path=d / "p.json", values_path=d / "v.yaml",
                    scenarios_path=d / "sc.jsonl", preferences_path=d / "pf.jsonl",
                    ai_profile_path=d / "ai.json")

    # 抽出提示的规模
    prompt = EX.EXTRACT_TEMPLATE.format(user="我妹妹叫林小雨", reply="林小雨，我记住了。")
    print(f"\n抽取提示约 {len(prompt)} 字（连 system 一起进 prompt eval）")
    print("=" * 80)
    t_all = []
    for i, (user, reply) in enumerate(CASES, 1):
        t0 = time.time()
        info = EX.extract_turn(c.extractor, user, reply)
        dt = time.time() - t0
        t_all.append(dt)
        tag = "首次（含分配，别当稳态）" if i == 1 else "稳态"
        print(f"{i}. {user}")
        print(f"   → {info.get('relations')}　{dt:.1f}s　{tag}")
    steady = t_all[1:] or t_all
    print("=" * 80)
    print(f"首次 {t_all[0]:.1f}s　稳态均值 {sum(steady)/len(steady):.1f}s"
          f"（{len(steady)} 次）")
    print(f"结束时显存：{vram()}")
    try:
        mgr.stop(8083)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
