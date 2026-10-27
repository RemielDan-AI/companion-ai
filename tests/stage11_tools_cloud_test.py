#!/usr/bin/env python
"""阶段 11 工具接入与云端边界测试（项目改进v1 模块三 改进 5-9）。

用法：python tests/stage11_tools_cloud_test.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cloud import gateway  # noqa: E402
from skills import executors  # noqa: E402
from tools import builtin, executor  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "tools"
LICENSE_OK = {"allow": ["local", "network", "local_exec"], "command_allowlist": ["cmd.exe"],
              "timeout": 5, "retries": 1, "max_result_chars": 600}
# 允许执行命令但白名单为空：用来单独验证"白名单为空一律拒绝"
LICENSE_NO_CMD = {"allow": ["local", "network", "local_exec"], "command_allowlist": [],
                  "timeout": 5, "retries": 1, "max_result_chars": 600}
LICENSE_TIGHT = {"allow": ["local"], "command_allowlist": [], "timeout": 5, "retries": 1}


class MockApi(BaseHTTPRequestHandler):
    """假的外部 API：/ok 返回 JSON，/boom 返回 500。"""
    calls = 0

    def do_GET(self):
        MockApi.calls += 1
        if self.path.startswith("/ok"):
            body = json.dumps({"current_weather": {"temperature": 21.5,
                                                   "weathercode": 2}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")

    def log_message(self, *args):
        pass


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    checks: list[tuple[str, bool, str]] = []
    log_path = TMP / "tool_calls.jsonl"

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    def run(tool: dict, policy: dict, text: str = "", **kwargs) -> dict:
        executor.CALL_LOG = log_path          # 日志写到临时目录
        return executor.run_tool(tool, text, policy=policy, **kwargs)

    # ---- 1. 权限检查（改进 5） ----
    denied = run({"type": "local", "name": "echo"}, LICENSE_TIGHT, "", skill_kind="forbidden")
    check("禁止类技能不能调用任何工具", denied["blocked"] and "禁止类" in denied["detail"],
          denied["detail"])
    no_net = run({"type": "http", "url": "http://127.0.0.1:1/x", "permission": "network"},
                 LICENSE_TIGHT)
    check("权限没开时拒绝（network）", no_net["blocked"] and "权限" in no_net["detail"],
          no_net["detail"])
    empty = run({"type": "command", "command": ["cmd.exe", "/c", "echo hi"]}, LICENSE_NO_CMD)
    check("命令行白名单为空时一律拒绝", empty["blocked"] and "白名单" in empty["detail"],
          empty["detail"])
    allowed_cmd = run({"type": "command", "command": ["cmd.exe", "/c", "echo", "hi"]},
                      LICENSE_OK)
    check("白名单里的命令可以执行", allowed_cmd["ok"] and "hi" in allowed_cmd["text"],
          allowed_cmd["text"][:30])
    not_listed = run({"type": "command", "command": ["powershell", "-c", "echo hi"]},
                     LICENSE_OK)
    check("白名单外的命令被拒", not_listed["blocked"], not_listed["detail"])

    # ---- 2. 本地工具（local） ----
    ok_local = run({"type": "local", "name": "echo"}, LICENSE_OK, "你好")
    check("本地工具调用成功", ok_local["ok"] and ok_local["text"] == "你好", ok_local["text"])
    missing = run({"type": "local", "name": "不存在的工具"}, LICENSE_OK, "x")
    check("未注册的本地工具报错而不是崩", not missing["ok"] and "没注册" in missing["detail"],
          missing["detail"])

    # ---- 3. HTTP 工具 + 重试 + 兜底 ----
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    http_tool = {"type": "http", "url": f"http://127.0.0.1:{port}/ok", "permission": "network",
                 "pick": "current_weather.temperature", "timeout": 5, "retries": 1}
    got = run(http_tool, LICENSE_OK)
    check("HTTP 工具取到值（按路径取值）", got["ok"] and got["text"] == "21.5", got["text"])

    before = MockApi.calls
    boom = {"type": "http", "url": f"http://127.0.0.1:{port}/boom", "permission": "network",
            "timeout": 5, "retries": 2, "fallback": "没查到，回头再试。"}
    failed = run(boom, LICENSE_OK)
    check("失败会重试（共 3 次）", MockApi.calls - before == 3, f"调用 {MockApi.calls - before} 次")
    check("失败走兜底话术而不是编数据",
          not failed["ok"] and failed["text"] == "没查到，回头再试。", failed["text"])
    no_fallback = run({**boom, "fallback": ""}, LICENSE_OK)
    check("没兜底时返回空（交给拒绝话术）", not no_fallback["ok"] and not no_fallback["text"], "")

    # 4. MCP 现在已接入：默认不配服务器也**不开权限**，应被权限门拦住
    mcp = run({"type": "mcp", "server": "everything", "name": "echo"}, LICENSE_OK)
    check("MCP 默认被权限门拦住（要显式开 mcp 权限）",
          mcp["blocked"] and "mcp" in mcp["detail"], mcp["detail"])

    # ---- 5. 调用日志 ----
    records = executor.recent_calls(50, path=log_path)
    check("工具调用都写了日志",
          len(records) >= 8 and all("time" in r and "skill" in r for r in records),
          f"{len(records)} 条")
    check("日志里能看出成功与失败", any(r["ok"] for r in records)
          and any(not r["ok"] for r in records), "")

    # ---- 6. 技能集成：weather 技能真的会去查 ----
    builtin.weather = lambda text, **kw: "杭州现在多云，18℃"     # 不联网，替换成假数据
    class FakeDecision:
        kind = "skill"
        skill_id = "E001"
        executor = {"type": "tool", "tool": {"type": "local", "name": "weather"},
                    "prompt": "用上面查到的实时信息回答"}
    direct, block = executors.run_local(FakeDecision(), "杭州今天天气怎么样", None)
    check("天气技能会去调工具并把资料交给生成",
          direct is None and "杭州现在多云" in block, block[:40])

    builtin.weather = lambda text, **kw: (_ for _ in ()).throw(RuntimeError("网络断了"))
    fallback_tool = {"type": "local", "name": "weather", "fallback": "天气没查到，回头再试。"}
    class FailDecision(FakeDecision):
        executor = {"type": "tool", "tool": fallback_tool, "prompt": ""}
    direct2, block2 = executors.run_local(FailDecision(), "今天天气", None)
    check("工具失败时用兜底话术直接答复", direct2 == "天气没查到，回头再试。", str(direct2))

    # ---- 7. 云端默认关闭（改进 8/9） ----
    api_log = TMP / "api_calls.jsonl"
    config = gateway.load_config(TMP / "不存在的配置.yaml")
    check("云端默认关闭", config["enabled"] is False
          and config["allow_api_for_chat"] is False, str(gateway.status(config)))
    blocked = gateway.call("any-api", "帮我翻译这句", purpose="tool", config=config,
                           log_path=api_log)
    check("关闭状态下调用被拦并留痕",
          not blocked["ok"] and "默认关闭" in blocked["reason"]
          and gateway.recent_calls(5, path=api_log), blocked["reason"])

    # ---- 8. 打开之后：白名单、最小外发、脱敏、预算（改进 6/7） ----
    open_config = {"enabled": True, "allow_api_for_chat": False, "allow_api_for_tools": True,
                   "allowed_apis": ["translate"], "outbound_whitelist": ["task_text", "profile"],
                   "redact": True, "budget": {"max_calls_per_day": 2,
                                              "max_chars_per_call": 200}, "timeout": 5}
    chat_blocked = gateway.call("translate", "聊聊", purpose="chat", config=open_config,
                                log_path=api_log)
    check("开总开关但没开 allow_api_for_chat 时，对话仍不上云",
          not chat_blocked["ok"] and "allow_api_for_chat" in chat_blocked["reason"],
          chat_blocked["reason"])
    unknown = gateway.call("other-api", "聊聊", purpose="tool", config=open_config,
                           log_path=api_log)
    check("不在 allowed_apis 里的 API 被拒",
          not unknown["ok"] and "白名单" in unknown["reason"], unknown["reason"])

    sent: list[dict] = []
    result = gateway.call("translate", "我的手机号是 13812345678，帮我翻译成英文",
                          purpose="tool", config=open_config, log_path=api_log,
                          transport=lambda payload, api, timeout: sent.append(payload) or "ok")
    check("允许后能调通", result["ok"] and sent, str(result["reason"]))
    check("只发任务文本（画像/记忆等永远不出去）",
          set(sent[0]) == {"task_text"}, f"实际字段：{sorted(sent[0])}")
    check("外发内容已脱敏", "13812345678" not in sent[0]["task_text"]
          and "[手机号]" in sent[0]["task_text"], sent[0]["task_text"][:40])
    check("外发被记进审计日志",
          any(r.get("sent") and r.get("api") == "translate"
              for r in gateway.recent_calls(20, path=api_log)), "")

    too_long = gateway.call("translate", "很长" * 300, purpose="tool", config=open_config,
                            log_path=api_log, transport=lambda *a: "ok")
    check("超长内容按上限截断",
          len(too_long["outbound"]["task_text"]) <= 200, str(len(too_long["outbound"]["task_text"])))

    tiny_budget = {**open_config, "budget": {"max_calls_per_day": 1, "max_chars_per_call": 200}}
    gateway.call("translate", "第一次", purpose="tool", config=tiny_budget, log_path=api_log,
                 transport=lambda *a: "ok")
    over = gateway.call("translate", "第二次", purpose="tool", config=tiny_budget,
                        log_path=api_log, transport=lambda *a: "ok")
    check("超出每日预算被拦", not over["ok"] and "预算" in over["reason"], over["reason"])

    check("never-outbound 名单写死不可配置",
          "profile" in gateway.NEVER_OUTBOUND and "memory" in gateway.NEVER_OUTBOUND, "")

    server.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"\n通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
