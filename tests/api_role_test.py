"""钉住「角色可以走远程 API」这条链路，尤其是**外发网关不能绕过**。

## 为什么网关这条最要紧

走 API 意味着**对话内容、记忆块、人格提示都要发到外面**。
项目本来一直守着一条线：`cloud/gateway.py` 里写死了
`NEVER_OUTBOUND = (profile, memory, values, history, persona, ai_profile, episodic, semantic)`，
而且 `config/api.yaml` 默认 `enabled: false`（一个都出不去）。

如果"角色走 API"这条新路绕过了网关，等于**从后门把这条线拆掉**——
而且用户看不出来（界面上只是"换了个模型"）。

所以本测试的核心断言是：
    **只要网关不放行，API 角色就必须被判为"不可用"，请求也不许发出去。**

⚠ 本测试**不联网、不发任何真实请求**，只验证判定与拦截逻辑。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services import models_config as mc   # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 88)
    print("角色走 API（含外发网关守卫）")
    print("=" * 88)

    # ---- ① 默认全是本地 ----
    check(mc.SOURCES == ("local", "api"), "SOURCES = local / api", str(mc.SOURCES))
    for role in mc.ROLES:
        check(mc.source(role) == "local",
              f"默认 {mc.ROLE_LABELS[role]} 是本地", mc.source(role))
        check(mc.auth_headers(role) == {},
              f"本地 {mc.ROLE_LABELS[role]} 不带鉴权头")
        check(mc.gateway_blocked(role) == "",
              f"本地 {mc.ROLE_LABELS[role]} 不涉及外发", "")

    # ---- ② 配成 api 之后：地址/头/网关判定 ----
    orig = {k: (mc.load()["roles"].get("chat") or {}).get(k)
            for k in ("source", "api_base", "api_model", "api_key_env", "api_name")}
    try:
        mc.set_role_param("chat", "source", "api")
        mc.set_role_param("chat", "api_base", "https://example.invalid/v1")
        mc.set_role_param("chat", "api_model", "some-model")
        mc.set_role_param("chat", "api_key_env", "COMPANION_TEST_KEY")
        mc.set_role_param("chat", "api_name", "demo")

        check(mc.source("chat") == "api", "chat 已经切成 api", mc.source("chat"))
        check(mc.endpoint("chat") == "https://example.invalid/v1/chat/completions",
              "**endpoint 指向 API**", mc.endpoint("chat"))

        # 没有密钥 → 网关必须拒
        blocked = mc.gateway_blocked("chat")
        check(bool(blocked), "**没设密钥 → 拒绝外发**", blocked[:60])
        check(mc.is_served("chat") is False,
              "**被网关拒绝时 is_served 必须为 False**（界面不能显示「可用」）")

        # 设了密钥，但网关默认 enabled: false → 仍然拒
        import os
        os.environ["COMPANION_TEST_KEY"] = "sk-test-not-real"
        check(mc.auth_headers("chat") == {"Authorization": "Bearer sk-test-not-real"},
              "**设了密钥就有 Bearer 头**")
        blocked2 = mc.gateway_blocked("chat")
        check(bool(blocked2),
              "**网关 enabled=false → 仍然拒绝**（这是默认姿态，不能绕过）",
              blocked2[:70])
        check(mc.is_served("chat") is False, "仍然判为不可用")

        # ---- ③ Generator 真的会在发请求前被拦 ----
        from engine.generate import Generator
        gen = Generator(api_url=mc.endpoint("chat"))
        gen.auth_headers = mc.auth_headers("chat")
        gen.gate_check = (lambda: mc.gateway_blocked("chat"))
        try:
            gen.chat("测试一句话", remember=False)
            raised = ""
        except Exception as exc:
            raised = f"{type(exc).__name__}: {exc}"
        check("外发被拒绝" in raised,
              "**Generator 在发请求前就被拦下**（不是发出去失败）", raised[:80])
    finally:
        for k, v in orig.items():
            if v is None:
                continue
            mc.set_role_param("chat", k, v)
        mc.set_role_param("chat", "source", "local")
        import os
        os.environ.pop("COMPANION_TEST_KEY", None)

    check(mc.source("chat") == "local", "配置已复原为本地")


    # ---- ⑥ **界面里打开开关之后，外发是不是真的放行了** ----
    #
    # 这条是整条链路的"闭环验证"：
    #   用户在设置里勾「允许云端能力」+「允许用云端生成对话」+ 填白名单
    #   → gateway.blocked_reason() 变成空
    #   → is_served(role) 变成 True
    #   → Generator 真的发出请求（这里用一个必然连不上的地址，验证**不再是"被网关拒"**）
    try:
        from cloud import api_config
    except Exception:
        api_config = None
    if api_config is not None:
        backup = pathlib.Path("config/api.yaml").read_text(encoding="utf-8")
        orig2 = {k: (mc.load()["roles"].get("chat") or {}).get(k)
                 for k in ("source", "api_base", "api_model", "api_key_env", "api_name")}
        import os
        try:
            mc.set_role_param("chat", "source", "api")
            mc.set_role_param("chat", "api_base", "http://127.0.0.1:9/v1")
            mc.set_role_param("chat", "api_model", "m")
            mc.set_role_param("chat", "api_name", "demo")
            os.environ["COMPANION_TEST_KEY"] = "sk-x"
            mc.set_role_param("chat", "api_key_env", "COMPANION_TEST_KEY")

            # 开关全开 + 白名单
            api_config.set_bool("enabled", True)
            api_config.set_bool("allow_api_for_chat", True)
            api_config.set_list("allowed_apis", ["demo"])

            check(mc.gateway_blocked("chat") == "",
                  "**开关打开后网关放行**", repr(mc.gateway_blocked("chat")))
            check(mc.is_served("chat") is True,
                  "**放行后 is_served 变成可用**")

            from engine.generate import Generator
            gen = Generator(api_url=mc.endpoint("chat"))
            gen.auth_headers = mc.auth_headers("chat")
            gen.gate_check = (lambda: mc.gateway_blocked("chat"))
            err = ""
            try:
                gen.chat("测试", remember=False)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            check("外发被拒绝" not in err,
                  "**不再被网关拦**（这里是连不上，不是被拒）", err[:70])

            # 白名单不含它 → 又要拒
            api_config.set_list("allowed_apis", ["other"])
            check(bool(mc.gateway_blocked("chat")),
                  "**api_name 不在白名单 → 重新拒绝**",
                  mc.gateway_blocked("chat")[:50])
        finally:
            pathlib.Path("config/api.yaml").write_text(backup, encoding="utf-8")
            for k, v in orig2.items():
                if v is not None:
                    mc.set_role_param("chat", k, v)
            mc.set_role_param("chat", "source", "local")
            os.environ.pop("COMPANION_TEST_KEY", None)

    # ---- ④ 嵌入也支持走 API（但不能和对话共用同一套判定）----
    check(hasattr(mc, "auth_headers") and hasattr(mc, "embed_endpoint"),
          "嵌入也有自己的地址与鉴权入口")

    # ---- ⑤ 网关的 NEVER_OUTBOUND 不能被这次改动动到 ----
    try:
        from cloud import gateway
        need = ("profile", "memory", "values", "history", "persona")
        missing = [k for k in need if k not in gateway.NEVER_OUTBOUND]
        check(not missing,
              "**网关的永不外发清单原样保留**", str(gateway.NEVER_OUTBOUND))
        cfg = gateway.load_config()
        check(cfg.get("enabled") is False,
              "**外发总开关默认仍是关**（走 API 必须先手动开）", str(cfg.get("enabled")))
    except Exception as exc:
        check(False, "网关模块可读", f"{type(exc).__name__}: {exc}")

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
