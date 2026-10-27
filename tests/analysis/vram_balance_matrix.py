"""找一组「聊天 + 抽取器」都能跑得快的参数（4GB 卡上的平衡点）。

## 问题

`split_extractor_check.py` 实测：chat(ngl=15) + extract(ngl=99) 同开时，
抽取一次要 **8~28 秒**（单独跑只要 0.9s）。日志显示 prompt 47 tok/s、生成 9.45 tok/s
—— **这是 CPU 速度**：显存不够时 llama.cpp **不报错，只是静默降级**。

显存账（4GB 卡，基线占 173 MiB）：
    chat(ngl=15)   1442 MiB
    extract(ngl=99) 1447 MiB
    合计 2889 < 3900 —— **账面上装得下，实际却被判装不下**
    （Vulkan 需要连续块 / llama.cpp 估算偏保守，`common_fit_params` 失败后按 ngl 走但实际掉 CPU）

## 本脚本做什么

按矩阵逐个组合起服务，测**抽取稳态耗时**和显存，找「两边都不慢」的点。
每个组合：起 chat → 起 extract → 抽 3 次取后两次均值 → 停。

用法：
    python tests/analysis/vram_balance_matrix.py
    python tests/analysis/vram_balance_matrix.py 15,12 99,20,0        # 自定义矩阵
"""
from __future__ import annotations

import itertools
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.manager import (CHAT_DEFAULTS, EXTRACT_DEFAULTS, EXTRACT_MODEL_KEYWORD,
                              ServiceManager, port_free)   # noqa: E402

CASES = [
    ("我妹妹叫林小雨", "林小雨，我记住了。"),
    ("我同事叫陈思远，做后端的", "陈思远，记下了。"),
    ("我养了只狗叫阿黄", "阿黄，好名字。"),
]


def vram_mib() -> int:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15).stdout.strip()
        return int(float(out))
    except Exception:
        return -1


def main() -> int:
    chat_ngls = [15, 12]
    ext_ngls = [99, 20, 12, 0]
    if len(sys.argv) > 1:
        chat_ngls = [int(x) for x in sys.argv[1].split(",")]
    if len(sys.argv) > 2:
        ext_ngls = [int(x) for x in sys.argv[2].split(",")]
    ext_ctx = int(sys.argv[3]) if len(sys.argv) > 3 else 4096

    mgr = ServiceManager()
    chat_model = mgr.find_model("v1.3")
    ext_model = mgr.find_model(EXTRACT_MODEL_KEYWORD)
    print(f"聊天：{chat_model['file']}　抽取：{ext_model['file']}")
    print(f"抽取 ctx={ext_ctx}　基线显存 {vram_mib()} MiB")
    print("=" * 88)
    print(f"{'chat ngl':>9}{'ext ngl':>9}{'显存MiB':>10}{'抽取稳态':>10}   结论")
    print("-" * 88)

    rows = []
    for chat_ngl, ext_ngl in itertools.product(chat_ngls, ext_ngls):
        for p in (8080, 8083):
            if not port_free("127.0.0.1", p):
                try:
                    mgr.stop(p)
                except Exception:
                    pass
        time.sleep(2)
        try:
            cs = dict(CHAT_DEFAULTS); cs.update({"model": chat_model["path"],
                                                 "port": 8080, "ngl": chat_ngl})
            mgr.start(cs, wait=True, timeout=300)
            es = dict(EXTRACT_DEFAULTS)
            es.update({"model": ext_model["path"], "port": 8083,
                       "ngl": ext_ngl, "ctx": ext_ctx})
            mgr.start(es, wait=True, timeout=300)
        except Exception as exc:
            print(f"{chat_ngl:>9}{ext_ngl:>9}{'-':>10}{'-':>10}   ✗ 起服务失败：{exc}")
            continue

        used = vram_mib()
        # 直接调抽取器（ChatGenerator 只用来占显存）
        import app as A
        from cognition import extract as EX
        d = pathlib.Path(tempfile.mkdtemp())
        c = A.Companion(memory_path=d / "m.db", index_path=d / "i.json",
                        profile_path=d / "p.json", values_path=d / "v.yaml",
                        scenarios_path=d / "sc.jsonl", preferences_path=d / "pf.jsonl",
                        ai_profile_path=d / "ai.json")
        times, got = [], []
        for user, reply in CASES:
            t0 = time.time()
            info = EX.extract_turn(c.extractor, user, reply)
            times.append(time.time() - t0)
            got.append(1 if info.get("relations") else 0)
        steady = sum(times[1:]) / max(1, len(times[1:]))
        ok = all(got)
        if steady < 2.5:
            verdict = "✅ 两边都快"
        elif steady < 8:
            verdict = "○ 可用（抽取偏慢）"
        else:
            verdict = "✗ 抽取掉到 CPU，太慢"
        if not ok:
            verdict += "；抽取结果为空"
        print(f"{chat_ngl:>9}{ext_ngl:>9}{used:>10}{steady:>9.1f}s   {verdict}")
        rows.append((chat_ngl, ext_ngl, used, steady, verdict, ok))
        try:
            mgr.stop(8083); mgr.stop(8080)
        except Exception:
            pass

    print("=" * 88)
    good = [r for r in rows if r[4].startswith("✅")]
    okish = [r for r in rows if r[5] and r[3] < 8]
    if good:
        r = min(good, key=lambda x: x[3])
        print(f"推荐：chat ngl={r[0]}　extract ngl={r[1]}　"
              f"显存 {r[2]} MiB　抽取 {r[3]:.1f}s")
    elif okish:
        r = min(okish, key=lambda x: x[3])
        print(f"可接受的折中：chat ngl={r[0]}　extract ngl={r[1]}　"
              f"显存 {r[2]} MiB　抽取 {r[3]:.1f}s（偏慢但可用）")
    else:
        print("没有找到两边都快的组合——4GB 卡可能确实装不下两个服务，"
              "考虑抽取走 CPU 并把 prompt 再压小，或攒轮（extract_every）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
