"""验证「同一个模型配给多个角色时，不重复启动」的防呆（**要真起服务**）。

## 用户提的问题

「当在设置中设置任务为同一个模型完成，不要多次启动，相当于用同一个模型完成多种工作」

现实场景：判官用 9B 最准（召回 100%），用户很可能把「判官」也设成 9B。
如果不做防呆，就会起**两个 9B 实例 = 白白多吃 5.2 GB**，
而四个常驻本来只剩零点几 GB（实测五个全上只剩 0.03 GB，会换页）。

## 这个探针怎么做

    ① 把「判官」也设成 9B（和「思考模型」同一个文件）
    ② 起 9B（走 think 角色）
    ③ 看 `shared_with('judge')` 是否指向 think、`effective_port('judge')` 是否等于 8085
    ④ **关键**：`judge_endpoint()` 是否直接返回 8085（而不是去起 8084）
    ⑤ 进程数：**只应该有 1 个 llama-server**
    ⑥ 复原配置

用法：
    python tests/analysis/shared_role_probe.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services import autostart, models_config as mc   # noqa: E402


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
    print("共用模型防呆　（把判官也设成 9B，看会不会起两份）")
    print("=" * 88)

    orig_judge = str((mc.load()["roles"]["judge"] or {}).get("model") or "")
    think_model = str((mc.load()["roles"]["think"] or {}).get("model") or "")
    print(f"  思考模型：{think_model}")
    print(f"  判官原配置：{orig_judge}")
    print()

    ok = True
    try:
        # ① 判官改成和思考模型一样
        mc.set_role_param("judge", "model", think_model)
        same = mc.model_key("judge") == mc.model_key("think")
        print(f"  ① 判官和思考现在是同一个文件：{same}")
        ok = ok and same

        # ② 起 9B（走 think）
        r = autostart.start_think(verbose=False)
        print(f"  ② start_think → {r.get('action')}　{r.get('detail', '')[:60]}")
        time.sleep(2)
        print(f"     当前 llama-server 进程数：{n_servers()}")
        ok = ok and n_servers() == 1, ok

        # ③ 共用判定
        shared = mc.shared_with("judge")
        eff = mc.effective_port("judge")
        print(f"  ③ shared_with('judge') = {shared}（应为 think）")
        print(f"     effective_port('judge') = {eff}（应为 8085，**不是** 8084）")
        ok = ok and shared == "think" and eff == 8085

        # ④ 判官端点直接指向共用的那个
        ep = mc.judge_endpoint()
        print(f"  ④ judge_endpoint() = {ep}")
        print(f"     → 直接连 8085，**没有**另起 8084：{'✅' if ep and '8085' in ep else '✗'}")
        ok = ok and bool(ep and "8085" in ep)

        # ⑤ 进程数仍然只有 1
        print(f"  ⑤ 进程数仍是：{n_servers()}（目标 1）")
        ok = ok and n_servers() == 1

        # ⑥ running_roles 里判官应该也报"可用"
        running = mc.running_roles()
        print(f"  ⑥ running_roles: think={running.get('think')} judge={running.get('judge')}"
              f"（判官由 think 代劳，也该是 True）")
        ok = ok and running.get("judge") is True
    finally:
        mc.set_role_param("judge", "model", orig_judge)
        try:
            autostart.stop_started(verbose=False)
        except Exception:
            pass

    print()
    print("  复原后 judge 配置：", (mc.load()["roles"]["judge"] or {}).get("model"))
    print("=" * 88)
    print("✅ 防呆成立：同一个模型服务多个角色，只起一份" if ok else "✗ 有不符项，见上")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
