"""技能工具接入（项目改进v1 模块三 改进 5）。

技能除了内置执行器，还可以声明一个外部工具：

    executor:
      type: tool
      tool:
        type: http | local | command | mcp
        permission: network | local_exec | cloud
        url: https://...            # http
        method: GET                 # http
        params: {q: "{text}"}       # http，{text} 会替换成提取出的参数
        name: weather               # local：tools/builtin.py 里的函数名
        command: ["ffprobe", "-v"]  # command：必须命中 tools.yaml 的白名单
        timeout: 10
        retries: 1
        fallback: 查不到时的兜底话术

要点（对应验收标准）：
- 工具可插拔：新增工具不改主流程，只需加 YAML + （本地函数时）注册一个函数；
- 权限检查：每次调用先过 tools.yaml 的允许清单，禁止类技能（forbidden）永远不执行工具；
- 失败降级：重试 → 兜底话术 → 诚实拒绝，绝不编数据；
- 调用日志：logs/tool_calls.jsonl 记录技能、工具、参数、耗时、结果。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import time
import urllib.parse
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS_PATH = ROOT / "config" / "tools.yaml"
LOG_DIR = ROOT / "logs"
CALL_LOG = LOG_DIR / "tool_calls.jsonl"

DEFAULTS = {
    "allow": ["local", "network"],   # 允许的权限类别（command / cloud 默认不给）
    "command_allowlist": [],         # 命令行工具白名单（留空＝一个都不许）
    "timeout": 12,
    "retries": 1,
    "max_result_chars": 600,
}


class ToolError(RuntimeError):
    """工具调用失败（带人话原因）。"""


def load_policy(path: pathlib.Path | str | None = None) -> dict:
    path = pathlib.Path(path) if path else TOOLS_PATH
    policy = dict(DEFAULTS)
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            policy.update({k: v for k, v in loaded.items() if v is not None})
        except Exception:
            pass
    return policy


def _log(record: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record.setdefault("time", dt.datetime.now().isoformat(timespec="seconds"))
    try:
        with open(CALL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_calls(limit: int = 20, path: pathlib.Path | str | None = None) -> list[dict]:
    path = pathlib.Path(path) if path else CALL_LOG
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def check_permission(tool: dict, policy: dict | None = None,
                     skill_kind: str = "") -> str:
    """返回空串表示通过，否则返回拒绝原因。"""
    policy = policy or load_policy()
    if skill_kind == "forbidden":
        return "禁止类技能不允许调用任何工具"
    if not tool:
        return "技能没有声明工具"
    kind = str(tool.get("type") or "").strip()
    if kind not in ("http", "local", "command", "mcp"):
        return f"不支持的工具类型：{kind or '（空）'}"
    permission = str(tool.get("permission") or
                     {"command": "local_exec", "http": "network",
                      "mcp": "mcp"}.get(kind, "local"))
    allowed = policy.get("allow") or []
    if permission not in allowed:
        return f"权限「{permission}」未开启（config/tools.yaml 的 allow 里没有它）"
    if kind == "command":
        allowlist = policy.get("command_allowlist") or []
        first = (tool.get("command") or [""])[0]
        if not allowlist:
            return "命令行工具白名单为空，一律不许执行"
        if first not in allowlist:
            return f"命令「{first}」不在白名单里"
    return ""


def run_tool(tool: dict, text: str = "", policy: dict | None = None,
             skill_id: str = "", skill_kind: str = "") -> dict:
    """执行工具。返回 {ok, text, detail}；失败时 text 是兜底话术或空串。"""
    policy = policy or load_policy()
    started = time.time()
    reason = check_permission(tool, policy, skill_kind)
    if reason:
        _log({"skill": skill_id, "tool": tool.get("type") if tool else None,
              "ok": False, "detail": reason, "text": text[:120]})
        return {"ok": False, "text": "", "detail": reason, "blocked": True}

    timeout = float(tool.get("timeout") or policy.get("timeout") or 12)
    retries = int(tool.get("retries", policy.get("retries", 1)))
    last_error = ""
    for attempt in range(max(1, retries + 1)):
        try:
            result = _dispatch(tool, text, timeout, policy)
            result = str(result or "").strip()[: int(policy.get("max_result_chars", 600))]
            if not result:
                raise ToolError("工具返回空结果")
            _log({"skill": skill_id, "tool": tool.get("type"), "ok": True,
                  "attempt": attempt + 1, "ms": int((time.time() - started) * 1000),
                  "result": result[:200]})
            return {"ok": True, "text": result, "detail": "", "attempt": attempt + 1,
                    "blocked": False}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    fallback = str(tool.get("fallback") or "").strip()
    _log({"skill": skill_id, "tool": tool.get("type"), "ok": False,
          "ms": int((time.time() - started) * 1000), "detail": last_error})
    return {"ok": False, "text": fallback, "detail": last_error,
            "fallback": bool(fallback), "blocked": False}


def _mcp(tool: dict, text: str, timeout: float, policy: dict) -> str:
    """调用 MCP 服务器上的工具。

    工具定义写在技能的 executor.tool 里：
      type: mcp
      server: everything          # config/mcp.yaml 里的服务器名
      name: get-sum               # 工具名
      args: {a: 1, b: 2}          # 参数；值里的 {text} 会替换成用户输入
    """
    from tools import mcp_client

    server = str(tool.get("server") or "").strip()
    if not server:
        raise ToolError("mcp 工具缺少 server（要写 config/mcp.yaml 里的服务器名）")
    tool_name = str(tool.get("name") or "").strip()
    if not tool_name:
        raise ToolError("mcp 工具缺少 name（要调用的 MCP 工具名）")
    arguments = {str(k): (str(v).replace("{text}", text) if isinstance(v, str) else v)
                 for k, v in (tool.get("args") or {}).items()}
    try:
        client = mcp_client.get_client(server, policy=policy)
    except mcp_client.McpError as exc:
        raise ToolError(str(exc)) from exc
    # 服务器可以用 tools 白名单限制可调用的工具（空＝全部允许）
    spec = mcp_client.load_servers().get(server) or {}
    allow = spec.get("tools") or []
    if allow and tool_name not in allow:
        raise ToolError(f"工具「{tool_name}」不在服务器「{server}」的白名单里")
    try:
        return client.call(tool_name, arguments, timeout=timeout)
    except mcp_client.McpError as exc:
        raise ToolError(str(exc)) from exc


def _dispatch(tool: dict, text: str, timeout: float, policy: dict) -> str:
    kind = str(tool.get("type") or "")
    if kind == "http":
        return _http(tool, text, timeout)
    if kind == "local":
        return _local(tool, text)
    if kind == "command":
        return _command(tool, timeout)
    if kind == "mcp":
        return _mcp(tool, text, timeout, policy)
    raise ToolError(f"不支持的工具类型：{kind}")


def _http(tool: dict, text: str, timeout: float) -> str:
    url = str(tool.get("url") or "")
    if not url:
        raise ToolError("http 工具缺少 url")
    method = str(tool.get("method") or "GET").upper()
    params = {str(k): str(v).replace("{text}", text)
              for k, v in (tool.get("params") or {}).items()}
    headers = {str(k): str(v) for k, v in (tool.get("headers") or {}).items()}
    if method == "GET":
        query = urllib.parse.urlencode(params)
        full = url + ("&" if "?" in url else "?") + query if query else url
        request = urllib.request.Request(full, headers=headers)
    else:
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    path_expr = str(tool.get("pick") or "").strip()
    if path_expr:
        return _pick(raw, path_expr)
    return raw[:400]


def _pick(raw: str, path_expr: str) -> str:
    """从 JSON 里按点号路径取值，例如 current_weather.temperature。"""
    try:
        data = json.loads(raw)
    except Exception:
        return raw[:400]
    node = data
    for part in path_expr.split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except Exception:
                return raw[:400]
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return raw[:400]
    return "" if node is None else str(node)


def _local(tool: dict, text: str) -> str:
    from tools import builtin
    name = str(tool.get("name") or "").strip()
    func = getattr(builtin, name, None)
    if func is None or not callable(func):
        raise ToolError(f"本地工具「{name}」没注册")
    return func(text, **(tool.get("args") or {}))


def _command(tool: dict, timeout: float) -> str:
    cmd = tool.get("command") or []
    if not isinstance(cmd, list) or not cmd:
        raise ToolError("command 工具缺少命令数组")
    proc = subprocess.run([str(x) for x in cmd], capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise ToolError(f"命令退出码 {proc.returncode}：{proc.stderr.strip()[:120]}")
    return proc.stdout.strip()[:400]
