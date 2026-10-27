"""钉住「抽取器与聊天分离」：主路径、回落、接线位置。

## 为什么

抽取和聊天**共用一套权重**会被微调带偏——微调数据里没有抽取样本
（`weiyang_train.jsonl` 1085 条含抽取特征 0 条），于是名字被抽坏
（实测「林小雨」→「**林小记得**」；官方基座同题正确）。

所以抽取单独走一个小模型（默认 8083 的 Qwen3.5-2B）。
**关键安全属性**：抽取服务挂了必须能**逐次回落到聊天模型**，
否则记忆就不落库了——那比抽得差更糟。

本测试**不连模型、不起服务**（用桩），跑得快且稳定。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.extractor import DEFAULT_EXTRACT_URL, Extractor, extract_url  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


class FakeGen:
    def __init__(self, tag: str, fail: bool = False):
        self.tag = tag
        self.fail = fail
        self.calls = 0

    def complete(self, *a, **k) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.tag} 不可用")
        return f"来自 {self.tag}"


def main() -> int:
    print("=" * 76)
    print("① 主路径：抽取器可用时走它")
    print("=" * 76)
    p, f = FakeGen("extract"), FakeGen("chat")
    ex = Extractor(primary=p, fallback=f, verbose=False)
    out = ex.complete("s", "u")
    check(out == "来自 extract", "用了 primary", out)
    check(ex.last_used == "primary", "last_used=primary")
    check(f.calls == 0, "聊天模型没被调用")
    check(ex.fallback_count == 0, "回落次数 0")

    print()
    print("=" * 76)
    print("② 回落：抽取器不可用时自动走聊天模型（**记忆不能丢**）")
    print("=" * 76)
    p2, f2 = FakeGen("extract", fail=True), FakeGen("chat")
    ex2 = Extractor(primary=p2, fallback=f2, verbose=False)
    out2 = ex2.complete("s", "u")
    check(out2 == "来自 chat", "回落到聊天模型", out2)
    check(ex2.last_used == "fallback", "last_used=fallback")
    check(ex2.fallback_count == 1, "回落次数 1")
    ex2.complete("s", "u")
    check(ex2.fallback_count == 2, "再调一次继续回落（逐次判断，不是一次性）")

    print()
    print("=" * 76)
    print("③ primary 为 None（明确关掉抽取服务）时直接走 fallback")
    print("=" * 76)
    f3 = FakeGen("chat")
    ex3 = Extractor(primary=None, fallback=f3, verbose=False)
    check(ex3.complete("s", "u") == "来自 chat", "直接用 fallback")

    print()
    print("=" * 76)
    print("④ 两边都不可用 → 必须显式报错（不能静默返回空）")
    print("=" * 76)
    try:
        Extractor(primary=FakeGen("x", fail=True), fallback=None,
                  verbose=False).complete("s", "u")
        check(False, "两边都挂时应抛异常")
    except Exception as exc:
        check(True, "两边都挂时抛异常", type(exc).__name__)

    print()
    print("=" * 76)
    print("⑤ 地址解析：显式参数 > 环境变量 > 默认")
    print("=" * 76)
    import os
    check(extract_url("http://x/") == "http://x/", "显式参数优先")
    os.environ["COMPANION_EXTRACT_URL"] = "http://env/"
    try:
        check(extract_url() == "http://env/", "环境变量次之")
        check(extract_url("http://x/") == "http://x/", "显式仍压过环境变量")
    finally:
        os.environ.pop("COMPANION_EXTRACT_URL", None)
    check(extract_url() == DEFAULT_EXTRACT_URL, "都没有时用默认",
          DEFAULT_EXTRACT_URL)

    print()
    print("=" * 76)
    print("⑥ 接线位置（静态检查，防以后被改回共用聊天模型）")
    print("=" * 76)
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    check("self.extractor" in src, "Companion 上有 extractor")
    check("self.memory, self.extractor" in src,
          "remember_turn 用的是 self.extractor（不是 self.generator）")
    check("no_extract_service" in src, "支持 no_extract_service 对照开关")
    auto = (ROOT / "services" / "autostart.py").read_text(encoding="utf-8")
    check("ensure_extract_service" in auto, "autostart 里有抽取器拉起")
    for name in ("gui.py", "app.py", "pet_server.py"):
        s = (ROOT / name).read_text(encoding="utf-8")
        check("ensure_extract_service" in s, f"{name} 入口接上了")

    print()
    print("=" * 76)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
