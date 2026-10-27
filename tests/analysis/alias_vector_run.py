"""起嵌入服务跑别名校验（`alias_vector_probe.py`）——服务用完自动停。

单独拆一个是因为探针本身只关心"信号有没有用"，
起服务/停服务的样板不该混进去（照 `retrieval_bench_with_embed.py` 的做法）。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
from services.manager import ServiceManager                      # noqa: E402
from services.autostart import EMBED_DEFAULTS                    # noqa: E402


def find_embed_model(pat: str = "*bge*.gguf") -> str | None:
    # ⚠ 模型根目录以 `services.manager.DEFAULT_MODEL_ROOTS` 为准，
    #   不要自己另写一套（实测踩过：写死几个相对路径 → 找不到模型）。
    from services.manager import DEFAULT_MODEL_ROOTS
    for root in DEFAULT_MODEL_ROOTS:
        p = pathlib.Path(root)
        if not p.exists():
            continue
        for hit in sorted(p.rglob(pat)):
            # ⚠ **必须只认文件**（实测踩过）：`rglob("*base*")` 连**目录**一起匹配，
            #   而 `sorted()` 把目录 `bge-base-zh-v1.5\` 排在文件前面 →
            #   拿目录路径去加载模型，llama.cpp 报
            #   `failed to open GGUF file '...\bge-base-zh-v1.5' (Permission denied)`。
            #   那个 "Permission denied" 很有迷惑性，其实根本不是权限问题。
            if hit.is_file():
                return str(hit)
    return None


def main() -> int:
    # 本地可能有多个 bge（small / base），用 `ALIAS_VECTOR_MODEL` 指定匹配串来挑。
    # 例：ALIAS_VECTOR_MODEL="*base*" 就会选 bge-base。默认还是原来的行为。
    pat = os.environ.get("ALIAS_VECTOR_MODEL", "*bge*.gguf")
    model = find_embed_model(pat)
    if not model:
        print(f"找不到匹配 {pat} 的嵌入模型")
        return 1
    print(f"嵌入模型：{model}")
    spec = {**EMBED_DEFAULTS, "model": model, "port": 8081}
    mgr = ServiceManager()
    try:
        handle = mgr.start(spec, wait=True)
        print(f"启动结果：healthy={handle.get('healthy')} pid={handle.get('pid')}")
    except Exception as exc:
        print(f"启动失败：{type(exc).__name__}: {exc}")
        return 1
    try:
        # 跑哪个脚本由 `EMBED_SCRIPT`（或旧的 `ALIAS_VECTOR_SCRIPT`）指定：
        #   默认 alias_vector_probe.py（全局阈值那个问题）
        #   alias_vector_rank.py   → "辅助审查 2B 提议"的排序问题
        #   retrieval_bench.py     → **主检索路径**的标尺
        script = (os.environ.get("EMBED_SCRIPT")
                  or os.environ.get("ALIAS_VECTOR_SCRIPT")
                  or "alias_vector_probe.py")
        args = os.environ.get("EMBED_SCRIPT_ARGS", "").split()
        return subprocess.call([sys.executable, str(HERE / script)] + args,
                               cwd=str(ROOT))
    finally:
        try:
            mgr.stop(8081)
            print("已停止嵌入服务")
        except Exception as exc:
            print(f"停止失败：{exc}")


if __name__ == "__main__":
    raise SystemExit(main())


