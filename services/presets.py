"""模型服务预设（项目改进v1 模块四：可选模型与参数、可保存预设）。

预设存在 config/presets.json，形如：
{
  "presets": {
    "对话·基线": {"kind": "chat", "model": "C:\\ai\\models\\...gguf", "port": 8080, ...},
    "嵌入·bge": {"kind": "embed", ...}
  },
  "default": "对话·基线"
}
"""
from __future__ import annotations

import json
import pathlib

from services.manager import CHAT_DEFAULTS, EMBED_DEFAULTS

PRESET_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "presets.json"

# 不内置任何预设：预设全部由使用者自己添加（改这里会恢复出厂项，一般不需要）。
BUILTIN: dict[str, dict] = {}

_PARAM_KEYS = ("kind", "model", "host", "port", "ctx", "ngl", "threads", "kv",
               "batch", "ubatch", "parallel", "jinja", "extra")


def clean(spec: dict) -> dict:
    """只保留已知参数，避免把界面上的临时字段写进预设。"""
    out = {k: spec[k] for k in _PARAM_KEYS if k in spec}
    out["port"] = int(out.get("port") or 8080)
    for key in ("ctx", "ngl", "threads", "batch", "ubatch", "parallel"):
        if out.get(key) not in (None, ""):
            out[key] = int(out[key])
    out["jinja"] = bool(out.get("jinja", out.get("kind") != "embed"))
    out["extra"] = str(out.get("extra") or "")
    return out


class PresetStore:
    def __init__(self, path: pathlib.Path | str | None = None):
        self.path = pathlib.Path(path) if path else PRESET_PATH
        self.data = self.load()

    def load(self) -> dict:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except Exception:
                raw = {}
        else:
            raw = {}
        presets = raw.get("presets") or {}
        for name, spec in BUILTIN.items():          # 内置预设（默认为空，全部由使用者添加）
            presets.setdefault(name, dict(spec))
        first = next(iter(presets), "")
        return {"presets": presets, "default": raw.get("default") or first}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def names(self) -> list[str]:
        return list(self.data["presets"])

    def get(self, name: str) -> dict:
        spec = self.data["presets"].get(name)
        if spec is None:
            raise KeyError(name)
        return dict(spec)

    def upsert(self, name: str, spec: dict) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("预设名不能为空")
        self.data["presets"][name] = clean(spec)
        self.save()

    def delete(self, name: str) -> bool:
        if name in self.data["presets"]:
            del self.data["presets"][name]
            if self.data.get("default") == name:
                self.data["default"] = next(iter(self.data["presets"]), "")
            self.save()
            return True
        return False

    def set_default(self, name: str) -> None:
        if name not in self.data["presets"]:
            raise KeyError(name)
        self.data["default"] = name
        self.save()

    def default(self) -> tuple[str, dict]:
        """默认预设；**一个预设都没有时返回 ("", {})**，不再抛错。"""
        name = self.data.get("default") or next(iter(self.data["presets"]), "")
        if not name or name not in self.data["presets"]:
            return "", {}
        return name, self.get(name)
