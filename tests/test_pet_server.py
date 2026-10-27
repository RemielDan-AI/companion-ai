# -*- coding: utf-8 -*-
"""pet_server.py（给桌宠用的接口）的测试。

分三层：
  1. 文本提取的纯逻辑；
  2. **HTTP 层端到端** —— 用注入的假 companion 走真实网络栈，
     覆盖路由、状态码、OpenAI 响应形状、错误路径。不依赖对话模型，
     所以能稳定跑；
  3. 真实模型（--live）：只有 8080 有服务时才跑，否则跳过。

跑法：
    python tests/test_pet_server.py           # 1 + 2
    python tests/test_pet_server.py --live    # 连真实模型
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pet_server import ChatService, _extract_text, start_background  # noqa: E402

LIVE = "--live" in sys.argv
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# ---------------------------------------------------------------- 假 companion

class FakeCompanion:
    """假的 Companion：只实现 say()，用来验证 HTTP 层而不惊动模型。"""

    def __init__(self):
        self.turn = 0
        self.seen: list[str] = []

    def say(self, text: str):
        self.turn += 1
        self.seen.append(text)
        return f"收到：{text}", {"level": "L2", "skill": "chat", "turn": self.turn}


print("=== 1. 文本提取（纯逻辑）===")
check("取最后一条 user 文本",
      _extract_text([{"role": "system", "content": "人设"},
                     {"role": "user", "content": "早上好"}]) == "早上好")
check("多条 user 时取最后一条",
      _extract_text([{"role": "user", "content": "第一句"},
                     {"role": "assistant", "content": "嗯"},
                     {"role": "user", "content": "第二句"}]) == "第二句")
check("多段 content 里的文本被拼起来",
      _extract_text([{"role": "user", "content": [
          {"type": "text", "text": "你好"},
          {"type": "text", "text": "在吗"}]}]) == "你好 在吗")
check("没有 user 消息返回空", _extract_text([{"role": "system", "content": "x"}]) == "")
check("空列表返回空", _extract_text([]) == "")
check("畸形项被跳过（不崩）",
      _extract_text([None, "字符串", {"role": "user", "content": "ok"}]) == "ok")
try:
    _extract_text([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}}]}])
    check("图片输入被明确拒绝", False, "没有抛异常")
except ValueError as exc:
    check("图片输入被明确拒绝（本地模型没有视觉能力）",
          "看不了图片" in str(exc), str(exc)[:50])

print()
print("=== 2. HTTP 层端到端（注入假 companion，走真实网络栈）===")
fake = FakeCompanion()
service = ChatService(companion=fake)
httpd, thread = start_background(port=18082, service=service)
base = "http://127.0.0.1:18082"


def call(path: str, payload=None, method: str | None = None):
    """发一个请求，返回 (状态码, 解析后的 JSON)。"""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"raw": body}


try:
    # --- OpenAI 兼容路径
    code, body = call("/v1/chat/completions", {
        "model": "companion-ai",
        "messages": [{"role": "user", "content": "你在吗"}]})
    check("POST /v1/chat/completions 返回 200", code == 200, str(code))
    check("是 chat.completion 形状", body.get("object") == "chat.completion",
          str(body.get("object")))
    choice = (body.get("choices") or [{}])[0]
    check("choices[0].message.content 有回复",
          choice.get("message", {}).get("content") == "收到：你在吗",
          str(choice.get("message")))
    check("finish_reason = stop", choice.get("finish_reason") == "stop")
    check("带上 companion 元信息（档位/技能）",
          (body.get("companion") or {}).get("skill") == "chat",
          str(body.get("companion")))
    check("原样回显请求的 model",
          body.get("model") == "companion-ai", str(body.get("model")))

    # --- 极简路径
    code, body = call("/chat", {"text": "晚安"})
    check("POST /chat 返回 200", code == 200, str(code))
    check("返回 reply", body.get("reply") == "收到：晚安", str(body.get("reply")))

    # --- 元信息接口
    code, body = call("/v1/models", method="GET")
    check("GET /v1/models 返回 200", code == 200, str(code))
    ids = [m["id"] for m in body.get("data", [])]
    check("模型清单里有 companion-ai", "companion-ai" in ids, str(ids))
    check("模型声明只支持文本（不谎报视觉）",
          all("image" not in (m.get("input") or []) for m in body.get("data", [])))

    code, body = call("/health", method="GET")
    # 8080 没跑时健康检查返回 503 —— 这是**如实报告**，不是 bug
    check("GET /health 有响应（200 就绪 / 503 依赖没起）", code in (200, 503), str(code))
    check("health 里有 chat_model 字段", "chat_model" in body, str(body))

    code, body = call("/", method="GET")
    check("GET / 列出可用端点", code == 200 and "endpoints" in body)

    # --- 错误路径
    code, body = call("/chat", {"text": ""})
    check("空文本返回 400", code == 400, str(code))
    check("错误信息可读", "不能为空" in str(body), str(body)[:80])

    code, body = call("/chat", {"text": "x" * 9000})
    check("超长文本返回 400", code == 400, str(code))
    check("说明太长", "太长" in str(body), str(body)[:80])

    code, body = call("/v1/chat/completions", {"messages": []})
    check("没有 user 文本返回 400", code == 400, str(code))

    code, body = call("/v1/chat/completions", {"messages": [
        {"role": "user", "content": [{"type": "image_url",
                                      "image_url": {"url": "x"}}]}]})
    check("发图片返回 400 且说明原因", code == 400, str(code))
    check("原因提到没有视觉能力", "看不了图片" in str(body), str(body)[:90])

    # 注意用 ASCII 路径：http.client 只接受 ASCII 的请求行
    # （中文路径要先 URL 编码，那是客户端的事，这里测的是服务端 404 分支）
    code, body = call("/nope/not-here", {"text": "x"})
    check("未知路径返回 404", code == 404, str(code))

    # --- 串行化：并发请求不该把回复搞乱
    results: list[str] = []
    lock = threading.Lock()

    def burst(index: int) -> None:
        _code, b = call("/chat", {"text": f"并发{index}"})
        with lock:
            results.append(b.get("reply", ""))

    threads = [threading.Thread(target=burst, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    check("6 个并发请求都拿到了自己的回复",
          len(results) == 6 and all(r.startswith("收到：并发") for r in results),
          f"{len(results)} 个")
    check("假 companion 的 say 被调用了 6 次",
          len([s for s in fake.seen if s.startswith("并发")]) == 6,
          str([s for s in fake.seen if s.startswith("并发")]))
finally:
    httpd.shutdown()
    httpd.server_close()

print()
if LIVE:
    print("=== 3. 真实模型（--live）===")
    real = ChatService()
    state = real.health()
    if not state["chat_model"]:
        print("  跳过：8080 上没有对话模型，先运行 python 启动模型.py")
    else:
        try:
            out = real.chat("用一句话打个招呼")
            check("真实模型能回话", bool(out["reply"].strip()), out["reply"][:70])
            if out["reply"]:
                print(f"      未央：{out['reply'][:100]}")
        except Exception as exc:
            check("真实模型能回话", False, f"{type(exc).__name__}: {exc}")
else:
    print("=== 3. 真实模型（已跳过；加 --live 才跑，需要 8080 上的对话模型）===")

print()
if failures:
    print(f"!! {len(failures)} 项未通过：")
    for f in failures:
        print(f"   - {f}")
    raise SystemExit(1)
print("全部通过")
