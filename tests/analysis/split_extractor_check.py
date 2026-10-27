"""单进程验证「抽取器与聊天分离」：主路径 / 回落 / 显存占用。

## 为什么要单进程

`ServiceManager` 把子进程绑进 `KILL_ON_JOB_CLOSE` 的 job object，
父进程一退，起的服务跟着走。所以要在一个进程里同时起
聊天（8080）+ 抽取器（8083）才能测回落。

## 要验证的三件事

    ① 抽取走 8083（Qwen3.5-2B），结果正确
    ② **杀掉 8083 后能回落到聊天模型**，记忆照常落库（不能因为抽取器挂了丢记忆）
    ③ **显存够不够**——4GB 卡上要同时装 chat + extract + embed，
       这是生产落地最现实的风险，必须量出来

用法：python tests/analysis/split_extractor_check.py
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

from services.manager import (CHAT_DEFAULTS, EXTRACT_DEFAULTS, ServiceManager,
                              port_free)          # noqa: E402


def vram() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free",
             "--format=csv,noheader"], capture_output=True, text=True,
            timeout=15).stdout.strip()
        return out or "?"
    except Exception:
        return "?"


def main() -> int:
    mgr = ServiceManager()
    started: list[int] = []

    def start(port: int, spec: dict, keyword: str) -> bool:
        if not port_free("127.0.0.1", port):
            print(f"  端口 {port} 已被占，跳过启动")
            return False
        mod = mgr.find_model(keyword)
        if not mod:
            print(f"  找不到模型 {keyword}")
            return False
        s = dict(spec)
        s.update({"model": mod["path"], "port": port})
        try:
            h = mgr.start(s, wait=True, timeout=300)
            started.append(port)
            print(f"  已起 {port}：{mod['file']}（PID {h.get('pid')}）"
                  f"　显存 {vram()}")
            return True
        except Exception as exc:
            print(f"  起 {port} 失败：{type(exc).__name__}: {exc}")
            return False

    print("=" * 84)
    print("① 同时起聊天（8080）与抽取器（8083）")
    print("=" * 84)
    print(f"  起之前显存：{vram()}")
    chat_ngl = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    ext_ngl = int(sys.argv[2]) if len(sys.argv) > 2 else 99
    print(f"  chat ngl={chat_ngl}　extract ngl={ext_ngl}")
    print(f"  （生产默认 chat ngl=15 是**故意**的：留显存给游戏）")

    chat_spec = dict(CHAT_DEFAULTS); chat_spec["ngl"] = chat_ngl
    ext_spec = dict(EXTRACT_DEFAULTS); ext_spec["ngl"] = ext_ngl
    ok_chat = start(8080, chat_spec, "v1.3")
    ok_ext = start(8083, ext_spec, "Qwen3.5-2B")
    if not ok_chat:
        print("\n聊天模型没起来，本测试没法继续（回落需要它在）")
        return 1

    print()
    print("=" * 84)
    print("② 抽取走 8083")
    print("=" * 84)
    d = pathlib.Path(tempfile.mkdtemp())
    import app as A
    from cognition import extract as EX
    c = A.Companion(memory_path=d / "m.db", index_path=d / "i.json",
                    profile_path=d / "p.json", values_path=d / "v.yaml",
                    scenarios_path=d / "sc.jsonl", preferences_path=d / "pf.jsonl",
                    ai_profile_path=d / "ai.json")
    t0 = time.time()
    i1 = EX.extract_turn(c.extractor, "我妹妹叫林小雨", "林小雨，我记住了。")
    dt1 = time.time() - t0
    print(f"  relations = {i1.get('relations')}　last_used={c.extractor.last_used}"
          f"　耗时 {dt1:.1f}s")

    print()
    print("=" * 84)
    print("③ 杀掉 8083，验证回落（记忆不能丢）")
    print("=" * 84)
    if ok_ext:
        h = mgr.services.get(8083) or {}
        proc = h.get("proc")
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                pass
            print("  已杀掉 8083")
    time.sleep(2)
    t0 = time.time()
    i2 = EX.extract_turn(c.extractor, "我养了只猫叫团子", "团子这名字好听。")
    dt2 = time.time() - t0
    print(f"  relations = {i2.get('relations')}　last_used={c.extractor.last_used}"
          f"　耗时 {dt2:.1f}s　回落次数={c.extractor.fallback_count}")

    print()
    print("=" * 84)
    print("结论")
    print("=" * 84)
    a = bool(i1.get("relations"))
    b = bool(i2.get("relations"))
    print(f"  主路径（8083）拿到结果：{'✅' if a else '✗'}　{i1.get('relations')}")
    print(f"  回落（8080）拿到结果：  {'✅' if b else '✗'}　{i2.get('relations')}")
    print(f"  显存（chat+extract 同开）：{vram()}")
    print()
    if a and b:
        print("  ✅ 分离方案成立：抽取器挂了也不会丢记忆")
    elif a:
        print("  ⚠ 主路径 OK，但回落没拿到结果——检查聊天模型是否可用")
    else:
        print("  ✗ 主路径就不通，先查 8083")

    for p in started:
        try:
            mgr.stop(p)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
