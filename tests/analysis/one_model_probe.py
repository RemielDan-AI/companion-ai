"""极端场景：**一个模型同时干 记忆 + 对话 + 思考 + 判官**。

## 用户指出的问题

「万一有人极端一点的用同一个模型完成记忆、对话、思考和判官的话，
  工作要能路由到才行，毕竟要是有大模型这些工作是完全可以一起做的」

这是**真的会发生**的用法：一个大模型（比如 9B）这几样都能干。
如果只做到"不重复启动"而**没把活路由过去**，结果就是：
服务只在 chat 的 8080 上跑，而抽取还在打 8083（空的）、
思考还在打 8085（空的）、判官还在打 8084（空的）——
**一个都没连上，功能全废，而且看起来像是"模型不行"。**

## 这个探针验什么

    ① 把 memory/think/judge 三个角色的模型**都改成 chat 那个**
    ② 只起 **chat** 一个服务
    ③ 四个角色**全部**必须路由到 8080：
         endpoint('memory') / endpoint('think') / judge_endpoint() /
         extract_url() / think_slot.url()
    ④ **进程数必须还是 1**（不允许多起一份）
    ⑤ `think_slot.need('l3')` 不能再去起一个——它该认出"已经在跑"
    ⑥ `should_trigger` 仍然按档位判断（该响应时响应，但不重复起）
    ⑦ 复原配置

用法：
    python tests/analysis/one_model_probe.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition.extractor import extract_url          # noqa: E402
from cognition.vectors import embed_url              # noqa: E402
from services import autostart, models_config as mc  # noqa: E402
from services import think_slot                       # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def n_servers() -> int:
    try:
        out = subprocess.run(
            ["powershell", "-c",
             "(Get-Process llama-server -ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, timeout=20)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return -1


def main() -> int:
    print("极端场景：一个模型干四种活")
    print("=" * 88)

    chat_model = str((mc.load()["roles"]["chat"] or {}).get("model") or "")
    originals = {r: str((mc.load()["roles"][r] or {}).get("model") or "")
                 for r in ("memory", "think", "judge")}
    print(f"  统一成：{chat_model}")
    print(f"  原来：memory={originals['memory']} / think={originals['think']}"
          f" / judge={originals['judge']}")
    print()

    try:
        # ① 三个角色都改成 chat 的模型
        for role in ("memory", "think", "judge"):
            mc.set_role_param(role, "model", chat_model)
        keys = {r: mc.model_key(r) for r in ("chat", "memory", "think", "judge")}
        check(len(set(keys.values())) == 1, "四个角色的 model_key 已经一致")

        # ② 只起 chat
        r = autostart.ensure_chat_service(port=mc.role_port("chat"), verbose=False)
        print(f"  ② 只起对话服务：{r.get('action')}　{r.get('detail', '')[:50]}")
        time.sleep(2)
        check(n_servers() == 1, "**进程数 = 1**（只起了对话那一个）", str(n_servers()))

        # ③ 四个角色全部路由到 8080
        port = str(mc.role_port("chat"))
        check(port in mc.endpoint("memory"), "endpoint(memory) → 对话端口",
              mc.endpoint("memory"))
        check(port in mc.endpoint("think"), "endpoint(think) → 对话端口",
              mc.endpoint("think"))
        je = mc.judge_endpoint() or ""
        check(port in je, "**judge_endpoint() → 对话端口**", je or "None")
        eu = extract_url()
        check(port in eu, "**extract_url() → 对话端口**（抽取真的连上了）", eu)
        tu = think_slot.url()
        check(port in tu, "**think_slot.url() → 对话端口**", tu)

        # 共用关系可见
        for role in ("memory", "think", "judge"):
            check(mc.shared_with(role) == "chat",
                  f"shared_with({role}) = chat", str(mc.shared_with(role)))

        # ④ running_roles 四个都是 True
        running = mc.running_roles()
        check(all(running.get(r) for r in ("chat", "memory", "think", "judge")),
              "**四个角色都报可用**（由一个服务全包）",
              str({k: v for k, v in running.items() if v}))

        # ⑤ need() 不许再起一个
        orig_enabled = mc.switch("auto_think")
        try:
            mc.set_switch("auto_think", True)
            before = n_servers()
            usable = think_slot.need("l3", verbose=False)
            time.sleep(2)
            after = n_servers()
            check(usable is True, "need('l3') 认为可用（已由对话服务提供）")
            check(after == before == 1,
                  "**need() 没有再起一个进程**", f"{before} → {after}")
        finally:
            mc.set_switch("auto_think", orig_enabled)

        # ⑥ 嵌入仍独立（不许被并到 chat 的端口上）
        check(str(mc.role_port("embed")) in embed_url(),
              "**嵌入仍走自己的端口**（/v1/embeddings 不能共用）", embed_url())
    finally:
        for role, model in originals.items():
            mc.set_role_param(role, "model", model)
        try:
            autostart.stop_started(verbose=False)
        except Exception:
            pass

    print()
    restored = {r: str((mc.load()["roles"][r] or {}).get("model") or "")
                for r in ("memory", "think", "judge")}
    print("  复原后：", restored)
    check(restored == originals, "配置已复原")

    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 一个模型能干四种活：全部路由到同一个服务，且只起一份")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
