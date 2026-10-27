# -*- coding: utf-8 -*-
"""对外接口：让桌宠（或别的本地程序）用 companion-ai 聊天。

为什么做成 **OpenAI 兼容**（``/v1/chat/completions``）而不是自定义协议：
桌宠、编辑器插件、脚本……现成的客户端全都认这个格式，接过来只要改 base_url，
不用为每个调用方各写一套。

**零新依赖**：只用标准库的 ``http.server``。companion-ai 原本就靠
urllib 做客户端，这里补一个服务端，不引入 Flask/FastAPI 这类框架。

两个接口：

* ``POST /v1/chat/completions`` —— OpenAI 格式（桌宠用这个）
* ``POST /chat``                 —— 极简格式 ``{"text": "..."}``（脚本/curl 用）
* ``GET  /v1/models``            —— 列出可选模型名（让客户端能"选模型"）
* ``GET  /health``               —— 自身与依赖服务是否就绪

**串行化**：``Companion`` 是有状态的会话对象（短期上下文、记忆注入状态），
并发调用会把上下文搅乱，所以服务端用一把锁让请求排队。
"""
from __future__ import annotations

import json
import logging
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8082          # 8080 是对话模型、8081 是嵌入，这里排到 8082
MODEL_NAME = "companion-ai"  # 对外就报这一个"模型名"
MAX_TEXT_CHARS = 8000        # 单条输入上限，防止误把整份文件贴进来


class ChatService:
    """把 Companion 包成线程安全的聊天服务（HTTP 层与业务层分开，便于单测）。"""

    def __init__(self, companion=None, lock: threading.Lock | None = None):
        if companion is None:
            from app import Companion
            companion = Companion()
        self.companion = companion
        self.lock = lock or threading.Lock()
        self.started_at = time.time()
        self.requests = 0
        self.reply, self.error = "", ""

    # ---- 业务

    def chat(self, text: str) -> dict:
        """跑一轮对话。返回 ``{"reply": str, "info": dict}``。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("text 不能为空")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"text 太长（{len(text)} > {MAX_TEXT_CHARS} 字）")
        with self.lock:                      # Companion 有状态，必须串行
            reply, info = self.companion.say(text)
            self.requests += 1
            self.reply = reply
            return {"reply": reply, "info": info or {}}

    def health(self) -> dict:
        """自身 + 依赖服务的健康状况。"""
        chat_ok = embed_ok = False
        detail = ""
        try:
            from services.manager import ServiceManager
            manager = ServiceManager()
            chat_ok = bool(manager.health(8080).get("ok"))
            embed_ok = bool(manager.health(8081).get("ok"))
        except Exception as exc:
            detail = f"服务探测失败：{exc}"
        return {
            "ok": chat_ok,                   # 对话模型不在就等于不能用
            "chat_model": chat_ok,
            "embed": embed_ok,
            "turns": getattr(self.companion, "turn", 0),
            "requests": self.requests,
            "uptime": round(time.time() - self.started_at, 1),
            "detail": detail,
        }

    def models(self) -> list[dict]:
        return [{"id": MODEL_NAME, "object": "model",
                 "owned_by": "companion-ai", "input": ["text"]}]


# ---------------------------------------------------------------- HTTP


def _extract_text(messages: list) -> str:
    """从 OpenAI 的 messages 里取出"这一轮要说的那句"。

    Companion 自己管短期上下文，所以只取**最后一条 user 消息**；
    客户端发来的 history 不用（否则两边的上下文会打架，记忆会被重复计数）。
    """
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):        # 多段内容（含图片）也会走到这
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif isinstance(item, dict) and item.get("type") == "image_url":
                    # 本地 4B 模型看不了图 —— 明确告诉调用方，别让它以为发出去了
                    raise ValueError(
                        "companion-ai 的本地模型看不了图片（没有视觉能力）；"
                        "要让它看图得另接一个视觉模型")
            return " ".join(parts).strip()
    return ""


class _Handler(BaseHTTPRequestHandler):
    service: ChatService = None          # 由 serve() 注入
    server_version = "companion-ai-pet/1.0"

    # ---- 工具

    def log_message(self, fmt, *args):   # 降低噪音：走 logging，不直接写 stderr
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass                          # 客户端先跑了，不算错

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:
            raise ValueError(f"请求体不是 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体应该是一个 JSON 对象")
        return data

    # ---- 路由

    def do_GET(self) -> None:             # noqa: N802 (BaseHTTPRequestHandler 约定)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/health", "/v1/health"):
            state = self.service.health()
            self._send(200 if state["ok"] else 503, state)
            return
        if path == "/v1/models":
            self._send(200, {"object": "list", "data": self.service.models()})
            return
        if path == "/":
            self._send(200, {"name": "companion-ai", "endpoints": [
                "POST /v1/chat/completions", "POST /chat",
                "GET /health", "GET /v1/models"]})
            return
        self._send(404, {"error": {"message": f"没有这个路径：{path}",
                                   "type": "not_found"}})

    def do_POST(self) -> None:            # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            data = self._read_json()
        except ValueError as exc:
            self._send(400, {"error": {"message": str(exc), "type": "invalid_request"}})
            return

        try:
            if path == "/chat":
                text = str(data.get("text") or "")
                result = self.service.chat(text)
                self._send(200, result)
                return

            if path == "/v1/chat/completions":
                text = _extract_text(data.get("messages") or [])
                if not text:
                    self._send(400, {"error": {
                        "message": "messages 里没有可用的 user 文本",
                        "type": "invalid_request"}})
                    return
                result = self.service.chat(text)
                self._send(200, self._openai_shape(result, data))
                return

            self._send(404, {"error": {"message": f"没有这个路径：{path}",
                                       "type": "not_found"}})
        except ValueError as exc:
            self._send(400, {"error": {"message": str(exc), "type": "invalid_request"}})
        except Exception as exc:          # 生成失败等：如实告诉调用方
            logger.warning("接口出错：%s", exc)
            self._send(500, {"error": {"message": f"{type(exc).__name__}: {exc}",
                                       "type": "server_error"}})

    def _openai_shape(self, result: dict, request: dict) -> dict:
        """包成 OpenAI 的响应形状（客户端只认这个）。"""
        info = result.get("info") or {}
        return {
            "id": f"chatcmpl-companion-{int(time.time()*1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": str(request.get("model") or MODEL_NAME),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result["reply"]},
                "finish_reason": "stop",
            }],
            # 把 companion 自己的元信息（档位/技能/情绪/记忆）也带上，
            # 客户端想展示"这一轮的内情"就有数据可用
            "companion": info,
        }


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1",
          service: ChatService | None = None,
          quiet: bool = False) -> ThreadingHTTPServer:
    """起服务（**阻塞**）。调用方想后台跑就自己开线程，或用 ``start_background``。"""
    service = service or ChatService()
    handler = type("BoundHandler", (_Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    # 本机自用：不记录每个请求（省得刷屏），要排查就把日志级别开到 DEBUG
    if quiet:
        logging.getLogger(__name__).setLevel(logging.INFO)
    logger.info("companion-ai 接口已就绪：http://%s:%d（模型名 %s）",
                host, port, MODEL_NAME)
    logger.info("  POST /v1/chat/completions   OpenAI 兼容")
    logger.info("  POST /chat                   {\"text\": \"...\"}")
    logger.info("  GET  /health  /v1/models")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，关闭接口")
    finally:
        httpd.server_close()
    return httpd


def start_background(port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                     service: ChatService | None = None):
    """在后台线程起服务，返回 (httpd, thread)。

    给"想跟 GUI 一起跑"的场景用 —— 不带控制台也能提供接口。
    """
    service = service or ChatService()
    handler = type("BoundHandler", (_Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="companion-http",
                              daemon=True)
    thread.start()
    logger.info("companion-ai 接口已在后台就绪：http://%s:%d", host, port)
    return httpd, thread


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="companion-ai 对外接口（OpenAI 兼容）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--quiet", action="store_true", help="少打日志")
    args = parser.parse_args(argv)

    # 启动时自动拉起嵌入服务（8081），否则记忆检索一直退化在纯词法（BM25），
    # 且写入的记忆不会被向量化（没有回填，索引会静默落后）。
    # 放**入口**而不是 Companion.__init__：测试会构造 Companion，
    # 放类里会到处拉起 llama-server。起不来只打印一行，不挡住服务。
    try:
        from services.autostart import ensure_embed_service, report as report_embed
        report_embed(ensure_embed_service(), verbose=not args.quiet)
        # 记忆抽取器（8083 的 Qwen3.5-2B）：与聊天解耦，避免被微调带偏。
        from services.autostart import (ensure_extract_service,
                                        report as report_extract)
        report_extract(ensure_extract_service(verbose=not args.quiet),
                       verbose=not args.quiet, label="记忆抽取")
    except Exception as exc:
        if not args.quiet:
            print(f"[记忆检索] 自动拉起服务时出错（忽略）：{type(exc).__name__}: {exc}")

    service = ChatService()
    # 索引回填：把「库里有、索引里没有」的记忆补上向量（后台线程，不挡启动）。
    # 起因同上面：extract.py 把 add_texts 的异常全吞掉，但没有回填机制。
    try:
        service.companion.backfill_index(verbose=not args.quiet)
    except Exception as exc:
        if not args.quiet:
            print(f"[记忆索引] 回填时出错（忽略）：{type(exc).__name__}: {exc}")
    state = service.health()
    if not state["chat_model"]:
        logging.getLogger(__name__).warning(
            "对话模型（8080）没在跑 —— 接口会起得来但每次请求都会失败；"
            "先运行 python 启动模型.py")
    serve(port=args.port, host=args.host, service=service, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
