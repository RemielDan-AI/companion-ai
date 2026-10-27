"""`config/api.yaml` 的**注释保留写入**（给界面用）。

## 为什么要单独这一层

界面要能直接开关云端能力（不用手改 YAML），但 `config/api.yaml` 里写满了
"默认姿态是全部关闭""这些字段写死禁止外发"这类**说明**——
用 `yaml.safe_dump` 整份重写会把它们全抹掉（`models.yaml` 上已经踩过两次）。

所以这里只做**行级替换**：改哪一行就只动那一行，注释、缩进、顺序全保留。

## 支持三种形状

    enabled: false                  ← 顶层标量
    allowed_apis: []                ← 顶层**列表**
    budget:
      max_calls_per_day: 0          ← 嵌一层
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "api.yaml"


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if not s or any(ch in s for ch in '#:{}[],&*!|>\'"'):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _lines() -> list[str] | None:
    try:
        return CONFIG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    except Exception:
        return None


def _write(lines: list[str]) -> bool:
    try:
        CONFIG_PATH.write_text("".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


def _patch_scalar(key: str, value, section: str = "") -> bool:
    """改 `key:` 那一行的值。`section` 非空时只在该段（多缩进一层）里找。"""
    lines = _lines()
    if lines is None:
        return False
    in_section = not section
    sec_indent = 0
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if section:
            if not in_section:
                if indent == 0 and stripped.startswith(section + ":"):
                    in_section = True
                    sec_indent = indent
                continue
            if indent <= sec_indent:
                break                       # 出了这一段
        if indent != (sec_indent + 2 if section else 0):
            continue
        if stripped.startswith(key + ":"):
            body = stripped[len(key) + 1:]
            tail = ("  #" + body.split("#", 1)[1]) if "#" in body else ""
            lines[i] = line[:indent] + key + ": " + _fmt(value) + tail + "\n"
            found = True
            break
    if not found:
        return False                    # 找不到就**不写**（宁可失败也不乱改）
    return _write(lines)


def _patch_list(key: str, items: list[str]) -> bool:
    """把 `key:` 下的列表项整段替换成 `items`。空列表写成 `key: []`。"""
    lines = _lines()
    if lines is None:
        return False
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if (len(line) - len(line.lstrip())) == 0 and stripped.startswith(key + ":"):
            start = i
            break
    if start is None:
        return False
    head_indent = 0
    # 找到列表块的结束位置（最后一个 `- xxx` 行的下一行）
    end = start + 1
    last_item = start
    while end < len(lines):
        line = lines[end]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # 空行/注释：先记着继续看，但如果后面已经不是列表项就停
            end += 1
            continue
        if stripped.startswith("- "):
            last_item = end
            end += 1
            continue
        break
    # 生成新块：`key:` 行 + 每项 `  - item`
    key_line = " " * head_indent + key + ":"
    body = []
    if items:
        body = [key_line + "\n"] + [" " * (head_indent + 2) + "- " + _fmt(x) + "\n"
                                    for x in items]
    else:
        body = [key_line + " []\n"]
    # 如果原块后面跟着别的顶格键，保留它；否则到文件末尾
    tail = lines[last_item + 1:] if last_item + 1 <= len(lines) else []
    # 注意：原块可能夹着注释，这里整体替换，但**保留 key 那一行上方的注释**
    return _write(lines[:start] + body + tail)


def set_bool(key: str, value: bool, section: str = "") -> bool:
    return _patch_scalar(key, bool(value), section)


def set_int(key: str, value: int, section: str = "") -> bool:
    try:
        return _patch_scalar(key, int(value), section)
    except Exception:
        return False


def set_list(key: str, items: list[str]) -> bool:
    return _patch_list(key, list(items))


def snapshot() -> dict:
    """给界面读的一行状态（**不写盘**）。"""
    try:
        from cloud import gateway
        cfg = gateway.load_config()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        from services import models_config as mc
        api_roles = [r for r in mc.ROLES if mc.source(r) == "api"]
        blocked = {r: mc.gateway_blocked(r) for r in api_roles}
    except Exception:
        api_roles, blocked = [], {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "allow_chat": bool(cfg.get("allow_api_for_chat")),
        "allow_tools": bool(cfg.get("allow_api_for_tools")),
        "redact": bool(cfg.get("redact")),
        "allowed_apis": list(cfg.get("allowed_apis") or []),
        "max_calls": int((cfg.get("budget") or {}).get("max_calls_per_day") or 0),
        "api_roles": api_roles,
        "blocked": blocked,
    }
