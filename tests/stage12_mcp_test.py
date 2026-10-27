#!/usr/bin/env python
"""阶段 12 MCP 客户端测试（模块三 改进 5 延伸）。

用一个真的假 MCP 服务器（本地脚本，stdio + JSON-RPC）验证：
握手、tools/list、tools/call、白名单、禁用与权限、超时、进程复用、调用日志。

用法：python tests/stage12_mcp_test.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import executor, mcp_client  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "mcp"

MOCK_SERVER = '''
import json
import sys
import time

TOOLS = [
    {"name": "echo", "description": "原样返回", "inputSchema": {"type": "object"}},
    {"name": "get-sum", "description": "求和", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "故意很慢", "inputSchema": {"type": "object"}},
]


def reply(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except Exception:
        continue
    method = message.get("method")
    if method == "initialize":
        reply({"jsonrpc": "2.0", "id": message["id"],
               "result": {"protocolVersion": "2024-11-05",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "mock", "version": "1.0"}}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        reply({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            reply({"jsonrpc": "2.0", "id": message["id"],
                   "result": {"content": [{"type": "text", "text": str(args.get("text", ""))}]}})
        elif name == "get-sum":
            total = int(args.get("a", 0)) + int(args.get("b", 0))
            reply({"jsonrpc": "2.0", "id": message["id"],
                   "result": {"content": [{"type": "text", "text": f"和是 {total}"}]}})
        elif name == "slow":
            time.sleep(5)
            reply({"jsonrpc": "2.0", "id": message["id"],
                   "result": {"content": [{"type": "text", "text": "慢工具完成"}]}})
        else:
            reply({"jsonrpc": "2.0", "id": message["id"],
                   "error": {"code": -32601, "message": f"没有工具 {name}"}})
'''


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    server_path = TMP / "mock_mcp_server.py"
    server_path.write_text(MOCK_SERVER, encoding="utf-8")
    config_path = TMP / "mcp.yaml"
    log_path = TMP / "tool_calls.jsonl"
    executor.CALL_LOG = log_path
    mcp_client.CONFIG_PATH = config_path

    def write_config(enabled: bool, tools: list[str] | None = None):
        config_path.write_text(json.dumps({
            "servers": {"mock": {"enabled": enabled, "command": sys.executable,
                                 "args": [str(server_path)], "tools": tools or [],
                                 "timeout": 20}}}, ensure_ascii=False), encoding="utf-8")

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    def call(tool: dict, text: str = "", policy: dict | None = None) -> dict:
        return executor.run_tool(tool, text, policy=policy or {"allow": ["mcp"]},
                                 skill_id="E900")

    # ---- 1. 权限与开关（双重门） ----
    write_config(True)
    blocked = call({"type": "mcp", "server": "mock", "name": "echo"},
                   policy={"allow": ["local"]})
    check("tools.yaml 没开 mcp 权限时被拒",
          blocked["blocked"] and "权限" in blocked["detail"], blocked["detail"])

    write_config(False)
    disabled = call({"type": "mcp", "server": "mock", "name": "echo"})
    check("服务器未启用时被拒",
          not disabled["ok"] and "未启用" in disabled["detail"], disabled["detail"])

    unknown_server = call({"type": "mcp", "server": "不存在的服务器", "name": "echo"})
    check("服务器名写错时给出人话原因",
          not unknown_server["ok"] and "没有配置" in unknown_server["detail"],
          unknown_server["detail"])

    # ---- 2. 真实握手与调用 ----
    write_config(True)
    mcp_client.close_all()
    started = time.time()
    echo = call({"type": "mcp", "server": "mock", "name": "echo", "args": {"text": "{text}"}},
                "你好 MCP")
    cost = time.time() - started
    check("MCP 调用成功且参数替换生效",
          echo["ok"] and echo["text"] == "你好 MCP", f"{echo['text']}（{cost:.1f}s）")

    summed = call({"type": "mcp", "server": "mock", "name": "get-sum",
                   "args": {"a": 3, "b": 4}})
    check("第二个工具也能调", summed["ok"] and "7" in summed["text"], summed["text"])

    client = mcp_client._CLIENTS.get("mock")
    check("进程被复用（没有每个工具都重启）",
          client is not None and client.proc is not None
          and client.proc.poll() is None, f"PID {client.proc.pid if client else '无'}")

    started = time.time()
    call({"type": "mcp", "server": "mock", "name": "echo", "args": {"text": "再来一次"}})
    check("复用时延明显更低（不用重新握手）", time.time() - started < cost,
          f"{time.time() - started:.2f}s < {cost:.2f}s")

    # ---- 3. 白名单与错误 ----
    write_config(True, tools=["echo"])
    mcp_client.close_all()
    not_allowed = call({"type": "mcp", "server": "mock", "name": "get-sum",
                        "args": {"a": 1, "b": 1}})
    check("服务器白名单外的工具被拒",
          not not_allowed["ok"] and "白名单" in not_allowed["detail"], not_allowed["detail"])

    write_config(True)
    mcp_client.close_all()
    no_such = call({"type": "mcp", "server": "mock", "name": "没有这个工具"})
    check("调用不存在的工具会报错而不是崩",
          not no_such["ok"] and "没有工具" in no_such["detail"], no_such["detail"])

    # ---- 4. 超时 ----
    fast = call({"type": "mcp", "server": "mock", "name": "slow", "timeout": 1,
                 "retries": 0})
    check("慢调用会超时并报原因",
          not fast["ok"] and "超时" in fast["detail"], fast["detail"][:60])

    # ---- 5. 降级与日志 ----
    mcp_client.close_all()
    fallback = call({"type": "mcp", "server": "mock", "name": "slow", "timeout": 1,
                     "retries": 0, "fallback": "这个功能暂时用不了。"})
    check("失败时走兜底话术", fallback["text"] == "这个功能暂时用不了。", fallback["text"])
    records = executor.recent_calls(30, path=log_path)
    check("MCP 调用也记进工具日志",
          any(r["ok"] for r in records) and any(not r["ok"] for r in records),
          f"{len(records)} 条")

    # ---- 6. 配置默认全关 ----
    defaults = mcp_client.load_servers(ROOT / "config" / "mcp.yaml")
    check("项目自带配置里 MCP 默认一个都没启用",
          bool(defaults) and not any(spec.get("enabled") for spec in defaults.values()),
          "、".join(f"{k}={'on' if v.get('enabled') else 'off'}" for k, v in defaults.items()))

    mcp_client.close_all()
    check("关闭后进程真的退出了",
          all(c.proc is None for c in mcp_client._CLIENTS.values()) or not mcp_client._CLIENTS,
          "")

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
