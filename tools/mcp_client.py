"""MCP 客户端（项目改进v1 模块三 改进 5 的延伸）。

MCP（Model Context Protocol）用 stdio 上的 JSON-RPC 2.0 通信：
启动服务端子进程 → initialize 握手 → notifications/initialized →
tools/list 取工具清单 → tools/call 调用。消息按行分隔（每行一个 JSON）。

设计取向与云端能力一致：**默认全关**。要启用必须同时满足
1) config/mcp.yaml 里该服务器 enabled: true；
2) config/tools.yaml 的 allow 里有 mcp。
任一条不满足都不启动进程，只给出人话原因。
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import subprocess
import threading
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "mcp.yaml"

PROTOCOL_VERSION = "2024-11-05"


class McpError(RuntimeError):
    """MCP 调用失败（带人话原因）。"""


def load_servers(path: pathlib.Path | str | None = None) -> dict:
    path = pathlib.Path(path) if path else CONFIG_PATH
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    servers = data.get("servers") or {}
    return {name: dict(spec or {}) for name, spec in servers.items()}


class McpClient:
    """一个 MCP 服务端的连接（进程 + 读写线程）。"""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.proc: subprocess.Popen | None = None
        self.inbox: queue.Queue = queue.Queue()
        self.next_id = 1
        self.tools: list[dict] = []
        self.timeout = float(spec.get("timeout") or 30)

    # ---- 生命周期 ----
    def start(self) -> None:
        command = self.spec.get("command")
        if not command:
            raise McpError(f"MCP 服务器「{self.name}」没配 command")
        argv = [str(command)] + [str(a) for a in (self.spec.get("args") or [])]
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.spec.get("env") or {}).items()})
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL,
                                         env=env, creationflags=flags, text=True,
                                         encoding="utf-8", errors="replace", bufsize=1)
        except FileNotFoundError as exc:
            raise McpError(f"启动 MCP 服务器「{self.name}」失败：找不到 {command}") from exc
        except Exception as exc:
            raise McpError(f"启动 MCP 服务器「{self.name}」失败：{exc}") from exc
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "companion-ai", "version": "1.0"},
            })
            self._notify("notifications/initialized", {})
            self.tools = self._request("tools/list", {}).get("tools") or []
        except McpError:
            self.close()
            raise

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        self.proc = None

    # ---- 读写 ----
    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.inbox.put(json.loads(line))
            except Exception:
                continue

    def _send(self, payload: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise McpError(f"MCP 服务器「{self.name}」没在运行")
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception as exc:
            raise McpError(f"给 MCP 服务器「{self.name}」发消息失败：{exc}") from exc

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float | None = None):
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params})
        deadline = time.time() + float(timeout or self.timeout)
        while time.time() < deadline:
            try:
                message = self.inbox.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"] or {}
                raise McpError(f"MCP 返回错误：{error.get('message') or error}")
            return message.get("result") or {}
        if self.proc is not None and self.proc.poll() is not None:
            raise McpError(f"MCP 服务器「{self.name}」已退出（退出码 {self.proc.returncode}）")
        raise McpError(f"MCP 服务器「{self.name}」超时（{method}）")

    # ---- 对外能力 ----
    def list_tools(self) -> list[dict]:
        if self.proc is None:
            self.start()
        return list(self.tools)

    def call(self, tool_name: str, arguments: dict | None = None,
             timeout: float | None = None) -> str:
        names = {tool.get("name") for tool in self.tools}
        if names and tool_name not in names:
            raise McpError(f"MCP 服务器「{self.name}」没有工具「{tool_name}」")
        result = self._request("tools/call",
                               {"name": tool_name, "arguments": arguments or {}},
                               timeout=timeout)
        return self._extract_text(result)

    @staticmethod
    def _extract_text(result: dict) -> str:
        parts = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            raise McpError("MCP 工具没返回文本内容")
        return text


_CLIENTS: dict[str, McpClient] = {}


def get_client(name: str, path: pathlib.Path | str | None = None,
               policy: dict | None = None) -> McpClient:
    """按名字取（必要时启动）一个 MCP 客户端。进程会被复用。"""
    if name in _CLIENTS and _CLIENTS[name].proc is not None:
        return _CLIENTS[name]
    servers = load_servers(path)
    spec = servers.get(name)
    if spec is None:
        raise McpError(f"没有配置 MCP 服务器「{name}」（看 config/mcp.yaml）")
    if not spec.get("enabled"):
        raise McpError(f"MCP 服务器「{name}」未启用（config/mcp.yaml 里 enabled: false）")
    if policy is not None and "mcp" not in (policy.get("allow") or []):
        raise McpError("MCP 权限未开启（config/tools.yaml 的 allow 里要加 mcp）")
    client = McpClient(name, spec)
    client.start()
    _CLIENTS[name] = client
    return client


def close_all() -> None:
    for client in list(_CLIENTS.values()):
        client.close()
    _CLIENTS.clear()
