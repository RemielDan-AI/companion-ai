"""云端能力网关（项目改进v1 模块三 改进 6-9）。

三条铁律（对应验收标准）：
1. **默认关闭**：config/api.yaml 里 enabled: false 时，这个模块完全不工作；
   用云端生成对话回复还要额外开 allow_api_for_chat（默认 false）；
2. **只发最小内容**：外发前过字段白名单 + 脱敏，画像/记忆/价值观/对话历史/人格设定
   默认禁止外发（在代码里就不放进 payload）；
3. **可审计**：每次外发记 logs/api_calls.jsonl（发了什么、给谁、耗时、预估花费、是否被拦）。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "api.yaml"
LOG_DIR = ROOT / "logs"
CALL_LOG = LOG_DIR / "api_calls.jsonl"

DEFAULTS = {
    "enabled": False,                 # 总开关：默认关
    "allow_api_for_chat": False,      # 用云端生成对话回复：默认关
    "allow_api_for_tools": True,      # 云端只做非对话任务（翻译/摘要这类）
    "allowed_apis": [],               # 允许调用的 API 名称清单
    "outbound_whitelist": ["task_text"],
    "redact": True,
    "budget": {"max_calls_per_day": 0, "max_chars_per_call": 1200},
    "timeout": 20,
}

# 这些字段永远不出去（写死在代码里，配置也改不动）
NEVER_OUTBOUND = ("profile", "memory", "values", "history", "persona",
                  "ai_profile", "episodic", "semantic")

_REDACTIONS = (
    (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[邮箱]"),
    (re.compile(r"\b\d{15,18}\b"), "[证件号]"),
    (re.compile(r"\b\d{6,}\b"), "[数字]"),
)


def load_config(path: pathlib.Path | str | None = None) -> dict:
    path = pathlib.Path(path) if path else CONFIG_PATH
    config = json.loads(json.dumps(DEFAULTS))       # 深拷贝
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for key, value in loaded.items():
                if isinstance(value, dict) and isinstance(config.get(key), dict):
                    config[key].update(value)
                else:
                    config[key] = value
        except Exception:
            pass
    return config


def status(config: dict | None = None) -> dict:
    config = config or load_config()
    return {"enabled": bool(config.get("enabled")),
            "chat": bool(config.get("enabled") and config.get("allow_api_for_chat")),
            "tools": bool(config.get("enabled") and config.get("allow_api_for_tools")),
            "allowed_apis": config.get("allowed_apis") or [],
            "calls_today": calls_today(),
            "budget": config.get("budget") or {}}


def redact(text: str) -> str:
    """脱敏：手机号/邮箱/证件号/长数字串替换掉。"""
    for pattern, tag in _REDACTIONS:
        text = pattern.sub(tag, text or "")
    return text


def minimize(text: str, config: dict | None = None,
             fields: dict | None = None) -> dict:
    """把要外发的内容压成最小集合：只留任务本身，其余一概不带。"""
    config = config or load_config()
    payload: dict = {}
    whitelist = set(config.get("outbound_whitelist") or ["task_text"])
    if "task_text" in whitelist:
        limit = int((config.get("budget") or {}).get("max_chars_per_call", 1200) or 1200)
        body = (text or "").strip()[:limit]
        payload["task_text"] = redact(body) if config.get("redact", True) else body
    for key, value in (fields or {}).items():
        if key in NEVER_OUTBOUND:
            continue
        if key in whitelist and value is not None:
            payload[key] = redact(str(value)) if config.get("redact", True) else str(value)
    return payload


def blocked_reason(api_name: str, purpose: str = "tool",
                   config: dict | None = None,
                   log_path: pathlib.Path | str | None = None) -> str:
    """返回空串表示可以调，否则返回拒绝原因。"""
    config = config or load_config()
    if not config.get("enabled"):
        return "云端能力默认关闭（config/api.yaml 的 enabled: false）"
    if purpose == "chat" and not config.get("allow_api_for_chat"):
        return "用云端生成对话回复需要手动开启 allow_api_for_chat"
    if purpose == "tool" and not config.get("allow_api_for_tools"):
        return "云端工具能力被关闭（allow_api_for_tools: false）"
    allowed = config.get("allowed_apis") or []
    if allowed and api_name not in allowed:
        return f"API「{api_name}」不在 allowed_apis 白名单里"
    if not allowed:
        return "allowed_apis 为空，没有允许调用的 API"
    budget = config.get("budget") or {}
    max_calls = int(budget.get("max_calls_per_day", 0) or 0)
    used = calls_today(log_path)
    if max_calls and used >= max_calls:
        return f"今天的调用预算用完了（{used}/{max_calls}）"
    return ""


def calls_today(path: pathlib.Path | str | None = None) -> int:
    path = pathlib.Path(path) if path else CALL_LOG
    if not path.exists():
        return 0
    today = dt.date.today().isoformat()
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if str(record.get("time", "")).startswith(today) and record.get("sent"):
            count += 1
    return count


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


def _log(record: dict, path: pathlib.Path | str | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = pathlib.Path(path) if path else CALL_LOG
    record.setdefault("time", dt.datetime.now().isoformat(timespec="seconds"))
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def call(api_name: str, text: str, purpose: str = "tool", transport=None,
         config: dict | None = None, config_path: pathlib.Path | str | None = None,
         log_path: pathlib.Path | str | None = None,
         fields: dict | None = None) -> dict:
    """统一出口：先查白名单与预算，再最小化+脱敏，最后交给 transport 发送。

    transport(payload, api_name, timeout) -> str
    transport 为 None 时只做到"准备好外发内容"这一步（便于测试与审计）。
    """
    config = config or load_config(config_path)
    reason = blocked_reason(api_name, purpose, config, log_path)
    payload = minimize(text, config, fields)
    if reason:
        _log({"api": api_name, "purpose": purpose, "sent": False,
              "reason": reason, "outbound": payload}, log_path)
        return {"ok": False, "reason": reason, "outbound": payload, "text": ""}

    started = time.time()
    try:
        if transport is None:
            raise RuntimeError("没有配置云端 transport（当前只做本地准备与审计）")
        result = transport(payload, api_name, float(config.get("timeout") or 20))
    except Exception as exc:
        _log({"api": api_name, "purpose": purpose, "sent": True,
              "reason": f"{type(exc).__name__}: {exc}", "outbound": payload,
              "ms": int((time.time() - started) * 1000)}, log_path)
        return {"ok": False, "reason": str(exc), "outbound": payload, "text": ""}

    _log({"api": api_name, "purpose": purpose, "sent": True, "ok": True,
          "outbound": payload, "ms": int((time.time() - started) * 1000),
          "result_chars": len(str(result))}, log_path)
    return {"ok": True, "reason": "", "outbound": payload, "text": str(result)}
