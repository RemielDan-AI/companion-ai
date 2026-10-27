"""按**角色**管理五个本地模型：对话 / 嵌入 / 记忆 / 思考 / 判官。

## 为什么单开一层

配置散在两处会互相打架：
    · `services/manager.py` 里的 `CHAT_DEFAULTS / EMBED_DEFAULTS / EXTRACT_DEFAULTS`
      把"模型 + 端口 + ngl"写死在代码里；
    · 界面又必须让用户能改。
所以统一到 `config/models.yaml`（五个角色的 model/port/ngl），
`manager` 的默认值只当**兜底**，实际以这里的为准。

## 机会主义判官（用户定的规则）

判官**只能**是 `think`(9B) 或 `judge`(4B)，**不许**用 `memory`(2B)——实测：

    程序侧（数字+编前情）      抓到 0/6
    2B 兼职判官                召回 **50%**   ✘ 漏「你孩子上小学了吗」这种要推理的
    4B                        召回 83%
    9B                        召回 **100%**

`judge_prefer` 的顺序就是"谁在跑用谁"：**9B 在就用 9B，不看 4B**；
两个都不在就**返回 None**，调用方跳过判定（不为一个默认关的门常驻 5 GB）。
"""
from __future__ import annotations

import pathlib
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "models.yaml"

# 五个角色（顺序就是界面上显示的顺序）
ROLES = ("chat", "embed", "memory", "think", "judge")
ROLE_LABELS = {
    "chat": "对话模型",
    "embed": "嵌入模型",
    "memory": "记忆模型",
    "think": "思考模型",
    "judge": "判官",
}
# 判官的硬规则：**只能**这两个角色里的人当
JUDGE_OK = ("think", "judge")


# 五个角色的默认端口与 ngl（`config/models.yaml` 没写时的兜底）
DEFAULT_PORTS = {"chat": 8080, "embed": 8081, "memory": 8083,
                 "think": 8085, "judge": 8084}
DEFAULT_NGL = {"chat": 20, "embed": 0, "memory": 12, "think": 20, "judge": 0}
# ⚠ ngl=12 是 memory 角色的实测平衡点：纯 CPU(0) 比 12 慢 5 倍，
#   中间值（20）比两端都差——层在 GPU/CPU 之间来回搬的开销大于它省下的计算。


def _yaml():
    import yaml
    return yaml



def _set_value(value) -> str:
    """把值格式化成 YAML 行尾的样子（字符串不加引号，除非含特殊字符）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if not s or any(ch in s for ch in "#:{}[],&*!|>\'"):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _patch_lines(section: str, key: str, value) -> bool:
    """只改 `section.key` 那一个值（其余含注释原样保留）；**key 不存在就追加**。

    ## ⚠ 2026-10-27 修了一个控制流 bug（踩了两次的同一类错误）

    第一版长这样：

        for i, line in enumerate(lines):
            ...
            if indent <= sec_indent and stripped:
                break                 # 离开本段
            if stripped.startswith(key + ":"):
                ...; break            # 找到并写入
        else:
            return False              # 没找到

    **但"离开本段"用的也是 `break`** —— `for...else` 里只要有 `break` 就不进 `else`，
    于是"key 根本不存在"和"找到了并写入"在控制流上**无法区分**：
    函数返回 True、文件也写了盘，但内容一个字没变。
    症状：给角色加新字段（`source`/`api_base` 这种）永远存不进去，
    而且调用方以为成功了。→ 改用显式 `found` 标志。

    ## 追加行为

    `key` 不存在时**追加到本段末尾**（缩进跟随段内已有子键），
    这样才能新增字段。追加位置在本段最后一个非空行之后。
    """
    try:
        lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    except Exception:
        return False

    in_section = False
    sec_indent = 0
    child_indent = None
    last_body = None          # 本段最后一个"正文行"的下标（用于追加）
    found = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if not in_section:
            if indent == 0 and stripped.startswith(section + ":"):
                in_section = True
                sec_indent = indent
                rest = stripped[len(section) + 1:].strip()
                if rest:
                    return False          # 行内字典，行级替换不安全
                last_body = i
            continue
        # 离开本段
        if indent <= sec_indent:
            break
        if child_indent is None:
            child_indent = indent
        last_body = i
        if stripped.startswith(key + ":"):
            body = stripped[len(key) + 1:]
            tail = ("  #" + body.split("#", 1)[1]) if "#" in body else ""
            lines[i] = line[:indent] + key + ": " + _set_value(value) + tail + "\n"
            found = True
            break

    if not in_section:
        return False
    if not found:
        # 追加一个新键（缩进跟本段子键一致；空段就退到 2 空格）
        pad = " " * (child_indent if child_indent is not None else sec_indent + 2)
        if last_body is None:
            return False
        if not lines[last_body].endswith("\n"):
            lines[last_body] = lines[last_body] + "\n"
        lines.insert(last_body + 1, pad + key + ": " + _set_value(value) + "\n")

    try:
        CONFIG_PATH.write_text("".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


def _writable() -> str:
    """能不能安全写回配置：返回拒绝原因（空串 = 可以写）。

    ⚠ **解析失败时一律不许写**：那时 `load()` 给的是内置默认值，
      写回去等于把用户的配置抹成出厂设置（实测发生过一次）。
    """
    return _PARSE_ERROR


def _write_all(data: dict) -> None:
    """整份写回（**只在行级替换失败时兜底**；会丢注释，尽量别走到这）。

    ⚠ 解析失败的配置**拒绝整份写回**——那会把内置默认值当用户配置存下去。
    """
    if _PARSE_ERROR:
        raise RuntimeError(f"配置解析失败，拒绝覆盖写回：{_PARSE_ERROR}")



_LOAD_CACHE: dict = {"mtime": None, "data": None}
# ⚠ **解析失败标记**：`load()` 读不动文件时会返回内置默认值（保证程序能起）。
#   但**默认值绝不能被写回文件**——那等于把用户的配置全部抹成出厂设置。
#   实测踩过：配置里混进一个 BEL 控制字符 → yaml 解析失败 → 之后任何
#   `set_role_param` 都走 `_write_all(默认值)` → **memory/think/judge 的模型全被清空**。
_PARSE_ERROR: str = ""


def load() -> dict:
    """读配置；缺项用内置默认补齐（配置文件坏了也不能让程序起不来）。

    ⚠ **带缓存**：一次设置面板构建会调它十几次
      （每个 `resolve_model_path` / `shared_with` / `endpoint` 都会调），
      每次重新读盘 + 解析 25 ms → 白烧几百毫秒。

    ⚠⚠ 缓存键是 **(mtime_ns, size)**，不是裸 `st_mtime`。
      `st_mtime` 是**秒级**精度，而 Windows 上时间戳分辨率更粗——
      **同一个时间片内连写两次，第二次的 mtime 和第一次一样**，
      缓存不失效，读到的是**上一次的配置**。
      症状：改完配置"没生效"；连续写不同内容时读到旧值。
      （`engine/replies.py` 同因同修。）
    """
    try:
        _st = CONFIG_PATH.stat()
        mtime = (_st.st_mtime_ns, _st.st_size)
    except Exception:
        mtime = None
    if (mtime is not None and _LOAD_CACHE["mtime"] == mtime
            and _LOAD_CACHE["data"] is not None):
        return _LOAD_CACHE["data"]
    global _PARSE_ERROR
    try:
        data = _yaml().safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        _PARSE_ERROR = ""
    except Exception as exc:
        data = {}
        _PARSE_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"⚠ config/models.yaml 解析失败（**不会覆盖写回**，请修好文件）：{exc}")
    roles = data.get("roles") or {}
    merged: dict[str, dict] = {}
    for role in ROLES:
        item = dict(roles.get(role) or {})
        item.setdefault("port", DEFAULT_PORTS[role])
        item.setdefault("ngl", DEFAULT_NGL[role])
        item.setdefault("ctx", 4096)
        item.setdefault("resident", role in ("chat", "embed", "memory"))
        item.setdefault("model", "")
        merged[role] = item
    out = dict(data)
    out["roles"] = merged
    out.setdefault("judge_prefer", ["think", "judge"])
    out.setdefault("judge_forbidden", ["memory", "chat"])
    sw = dict(out.get("switches") or {})
    for key, val in DEFAULT_SWITCHES.items():
        sw.setdefault(key, val)
    out["switches"] = sw
    if mtime is not None:
        _LOAD_CACHE["mtime"] = mtime
        _LOAD_CACHE["data"] = out
    return out


DEFAULT_SWITCHES = {
    "autostart": True,            # 启动界面时自动拉起常驻角色
    "stop_on_exit": True,         # 关闭界面时停掉本次拉起的服务
    "opportunistic_judge": True,  # 有思考模型就用思考模型当判官
    "exclusive_think": True,      # 拉起思考模型前先关判官模型
}


# ⚠ **伪开关**：界面上的复选框统一走 `switch()/set_switch()`，
#   但有些开关天然属于别的段落（`auto_think.enabled` 在 `auto_think:` 下）。
#   不映射的话会分成两处（界面写 `switches.auto_think`、程序读 `auto_think.enabled`），
#   **勾了没反应**——实测踩过。
_ALIAS = {
    "auto_think": ("auto_think", "enabled"),
}


def switch(name: str) -> bool:
    """读一个开关；读不到就用默认（**绝不抛**）。"""
    try:
        data = load()
        if name in _ALIAS:
            section, key = _ALIAS[name]
            return bool((data.get(section) or {}).get(key, False))
        return bool((data.get("switches") or {}).get(name,
                                                     DEFAULT_SWITCHES.get(name, True)))
    except Exception:
        return bool(DEFAULT_SWITCHES.get(name, True))


def set_switch(name: str, value: bool) -> dict:
    """改一个开关并落盘（支持伪开关，见 `_ALIAS`）。

    ⚠ **行级替换**：整份 `yaml.safe_dump` 会抹掉文件里**所有注释**，
      而 `config/models.yaml` 是一份带实测记录的文档（资源账、召回数据、踩过的坑）。
      实测踩过一次：界面勾了一下开关，整份文档的注释全没了。
    """
    # 先更新内存里的数据（行级替换失败时用它兜底）
    _bad = _writable()
    if _bad:
        # 解析失败 → **绝不写**，返回当前（可能是默认值）但不落盘
        raise RuntimeError(f"config/models.yaml 解析失败，拒绝写入：{_bad}")
    data = load()
    if name in _ALIAS:
        section, key = _ALIAS[name]
        blob = dict(data.get(section) or {})
        blob[key] = bool(value)
        data[section] = blob
        ok = _patch_lines(section, key, bool(value))
    else:
        sw = dict(data.get("switches") or {})
        sw[name] = bool(value)
        data["switches"] = sw
        ok = _patch_lines("switches", name, bool(value))
    if not ok:
        _write_all(data)
    _LOAD_CACHE["mtime"] = None      # 写盘后让缓存失效
    return load()


def save(role_updates: dict) -> dict:
    """只改 `role_updates` 里给的字段（`{role: {k: v}}`），其余保持。

    同样走**行级替换**，保住注释。
    """
    _bad = _writable()
    if _bad:
        # 解析失败 → **绝不写**，返回当前（可能是默认值）但不落盘
        raise RuntimeError(f"config/models.yaml 解析失败，拒绝写入：{_bad}")
    data = load()
    all_ok = True
    for role, updates in (role_updates or {}).items():
        if role not in ROLES:
            continue
        cur = dict(data["roles"].get(role) or {})
        cur.update({k: v for k, v in (updates or {}).items() if v is not None})
        data["roles"][role] = cur
        for key, val in (updates or {}).items():
            if val is None:
                continue
            if not _patch_role_key(role, key, val):
                all_ok = False
    if not all_ok:
        _write_all(data)
    _LOAD_CACHE["mtime"] = None      # 写盘后让缓存失效
    return load()


def _patch_role_key(role: str, key: str, value) -> bool:
    """改 `roles.<role>.<key>`（三层缩进，用角色名当锚点）；**key 不存在就追加**。

    ⚠ 同 `_patch_lines`：原来"离开角色块"用 `break`，和"找到了"共用一个出口，
      `for...else` 区分不出来 → 给角色加新字段时**静默失败**。改用显式 `found`。
    """
    try:
        lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    except Exception:
        return False

    in_roles = False
    in_role = False
    role_indent = 0
    child_indent = None
    last_body = None
    found = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if not in_roles:
            if indent == 0 and stripped.startswith("roles:"):
                in_roles = True
            continue
        if indent == 0:
            break                       # 出了 roles 段
        if not in_role:
            if stripped == role + ":":
                in_role = True
                role_indent = indent
                last_body = i
            continue
        if indent <= role_indent:
            break                       # 出了这个角色
        if child_indent is None:
            child_indent = indent
        last_body = i
        if stripped.startswith(key + ":"):
            body = stripped[len(key) + 1:]
            tail = ("  #" + body.split("#", 1)[1]) if "#" in body else ""
            lines[i] = line[:indent] + key + ": " + _set_value(value) + tail + "\n"
            found = True
            break

    if not in_role:
        return False
    if not found:
        pad = " " * (child_indent if child_indent is not None else role_indent + 2)
        if last_body is None:
            return False
        if not lines[last_body].endswith("\n"):
            lines[last_body] = lines[last_body] + "\n"
        lines.insert(last_body + 1, pad + key + ": " + _set_value(value) + "\n")

    try:
        CONFIG_PATH.write_text("".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False



# ===========================================================================
# 共用判定（同一模型服务多个角色时只起一份）
# ===========================================================================
# 能承载"对话类"工作的角色。**embed 不在里面**——它提供的是 /v1/embeddings，
# 和 /v1/chat/completions 是两回事，绝不能共用一个服务。
CHAT_ROLES = ("chat", "memory", "think", "judge")


def model_key(role: str) -> str:
    """角色的模型身份：解析出的 .gguf 路径；解析不到就用关键词本身。

    ⚠ 用**路径**而不是关键词做主键：两个角色可能写不同关键词，
      但指向**同一个文件**——那也该共用。
    """
    path = resolve_model_path(role)
    if path:
        return pathlib.Path(path).resolve().as_posix().lower()
    cfg = (load()["roles"].get(role) or {})
    return str(cfg.get("model") or "").strip().lower()


def role_port(role: str) -> int:
    """角色**自己配置的**端口（不考虑共用）。"""
    cfg = (load()["roles"].get(role) or {})
    return int(cfg.get("port") or DEFAULT_PORTS.get(role, 8080))


def shared_with(role: str) -> str | None:
    """**防呆**：本角色的模型如果和另一个**已经在跑**的角色是同一个文件，
    返回那个角色名（意思是"别另起一份，用它的服务"）。
    """
    if role not in CHAT_ROLES:
        return None                      # embed 永远独立
    if source(role) == "api":
        return None                      # 走 API 的角色没有本地进程
    key = model_key(role)
    if not key:
        return None
    for other in CHAT_ROLES:
        if other == role:
            continue
        if source(other) == "api":
            continue
        if model_key(other) != key:
            continue
        if _alive(role_port(other)):
            return other
    return None


def effective_port(role: str) -> int:
    """这个角色**实际该连哪个端口**：共用 → 对方端口；否则 → 自己的。"""
    other = shared_with(role)
    return role_port(other) if other else role_port(role)


# ===========================================================================
# **角色可以走远程 API**（source: api）——但必须过外发网关
# ===========================================================================
#
# 为什么要把网关接在这里：走 API 意味着**对话内容、记忆、人格提示都要发出去**，
# 而 `cloud/gateway.py` 里已经写死了一批"永不外发"的字段
# （profile/memory/values/history/persona…）以及 `allow_api_for_chat` 开关。
# 如果角色走 API 却绕过网关，等于把项目一直守着的隐私边界**从后门拆掉**。
# 所以：**任何角色配成 api，每次调用前都要问一遍 `gateway.blocked_reason()`**，
# 默认配置（enabled: false）下**一律拒绝**。

SOURCES = ("local", "api")
SOURCE_LABELS = {"local": "本地模型", "api": "远程 API"}


def source(role: str) -> str:
    """这个角色是本地模型还是远程 API。"""
    cfg = (load()["roles"].get(role) or {})
    src = str(cfg.get("source") or "local").strip().lower()
    return src if src in SOURCES else "local"


def api_conf(role: str) -> dict:
    """API 相关配置（base / model / api_name / key）。

    ⚠ **API key 默认不写进配置文件**：优先读环境变量名 `api_key_env`，
      例如 `api_key_env: DEEPSEEK_API_KEY`。直接写 `api_key` 也支持，
      但那等于把密钥明文放在仓库里——界面上会提示风险。
    """
    cfg = (load()["roles"].get(role) or {})
    import os as _os
    key_env = str(cfg.get("api_key_env") or "").strip()
    key = _os.environ.get(key_env, "") if key_env else ""
    if not key:
        key = str(cfg.get("api_key") or "").strip()
    base = str(cfg.get("api_base") or "").strip().rstrip("/")
    return {
        "name": str(cfg.get("api_name") or role).strip(),
        "base": base,
        "model": str(cfg.get("api_model") or "").strip(),
        "key": key,
        "key_env": key_env,
        "configured": bool(base and (key or not key_env)),
    }


def api_endpoint(role: str, path: str = "/chat/completions") -> str:
    """拼出 API 的完整地址。"""
    base = api_conf(role)["base"]
    if not base:
        return ""
    if base.endswith("/v1"):
        return base + path
    if "/v1" in base:
        return base.rstrip("/") + path
    return base + "/v1" + path


def auth_headers(role: str) -> dict:
    """这个角色发请求时该带的鉴权头。本地角色返回空 dict。"""
    if source(role) != "api":
        return {}
    api = api_conf(role)
    return {"Authorization": f"Bearer {api['key']}"} if api["key"] else {}


def gateway_blocked(role: str) -> str:
    """走 API 前问网关：返回**拒绝原因**（空串 = 可以发）。本地角色永远空。"""
    if source(role) != "api":
        return ""
    try:
        from cloud import gateway
    except Exception:
        return "外发网关不可用，拒绝外发"
    api = api_conf(role)
    if not api["base"]:
        return f"{ROLE_LABELS.get(role, role)}配成了 API 但没填 api_base"
    if api["key_env"] and not api["key"]:
        return (f"环境变量 {api['key_env']} 里没有密钥"
                f"（把 API key 放进这个环境变量，别直接写在配置里）")
    # 所有角色都按 "chat" 处理：它们处理的都是**对话内容**
    return gateway.blocked_reason(api["name"], purpose="chat")


# ===========================================================================
# 路由出口
# ===========================================================================
def endpoint(role: str) -> str:
    """这个角色**该连的** chat/completions 地址。

    · `source: api` → 远程 API 地址
    · `source: local` → 本地端口（**共用时指向对方的端口**）

      `think_slot.url()` / `cognition/extractor.py` / `judge_endpoint()` 都走这里。
    """
    if source(role) == "api":
        return api_endpoint(role)
    return f"http://127.0.0.1:{effective_port(role)}/v1/chat/completions"


def embed_endpoint() -> str:
    """嵌入地址。**embed 永远不共用**（它是 /v1/embeddings），
    也支持 `source: api`（比如用云端的 embedding）。"""
    if source("embed") == "api":
        return api_endpoint("embed", "/embeddings")
    return f"http://127.0.0.1:{effective_port('embed')}/v1/embeddings"


def is_served(role: str) -> bool:
    """这个角色现在**有没有可用的服务**（自己的 / 共用的 / 远程 API）。

    ⚠ `source: api` 的角色**不去探端口**：配置齐了算可用，
      但**网关不许发的时候算不可用**——界面要看得出"配了但发不出去"。
    """
    if source(role) == "api":
        api = api_conf(role)
        if not api["configured"]:
            return False
        return not gateway_blocked(role)
    return _alive(effective_port(role))


def forget_alive(port: int | None = None) -> None:
    """清掉探活缓存。**起/停服务之后要调**，否则界面最多滞后 _ALIVE_TTL 秒。"""
    if port is None:
        _ALIVE_CACHE.clear()
    else:
        _ALIVE_CACHE.pop(int(port), None)


# ===========================================================================
# 写配置（**行级替换，保住注释**）
# ===========================================================================
def set_value(section: str, key: str, value) -> dict:
    """改**任意段落**里的一个键，走行级替换（保住注释）。

    给测试和脚本用——它们原来直接 `yaml.safe_dump` 整份写，把注释全抹掉了。
    """
    _bad = _writable()
    if _bad:
        # 解析失败 → **绝不写**，返回当前（可能是默认值）但不落盘
        raise RuntimeError(f"config/models.yaml 解析失败，拒绝写入：{_bad}")
    data = load()
    blob = dict(data.get(section) or {})
    blob[key] = value
    data[section] = blob
    if not _patch_lines(section, key, value):
        _write_all(data)
    _LOAD_CACHE["mtime"] = None          # 写盘后让缓存失效
    return load()


def set_role_param(role: str, key: str, value) -> dict:
    """改某个角色的一个参数（model/port/ngl/ctx/resident/source/api_*）。

    ⚠ key **不存在时会追加**（`_patch_role_key` 支持新增行），
      所以给角色加 `source`/`api_base` 这类新字段是能存进去的。
    """
    if role not in ROLES:
        return load()
    _bad = _writable()
    if _bad:
        # 解析失败 → **绝不写**，返回当前（可能是默认值）但不落盘
        raise RuntimeError(f"config/models.yaml 解析失败，拒绝写入：{_bad}")
    data = load()
    cur = dict(data["roles"].get(role) or {})
    cur[key] = value
    data["roles"][role] = cur
    if not _patch_role_key(role, key, value):
        _write_all(data)                 # 兜底（会丢注释，尽量避免）
    _LOAD_CACHE["mtime"] = None          # 写盘后让缓存失效
    return load()


def role_spec(role: str) -> dict:
    """给 `ServiceManager.start()` 用的完整 spec（含模型绝对路径）。"""
    cfg = load()["roles"].get(role) or {}
    spec = {
        "kind": "embed" if role == "embed" else "chat",
        "host": "127.0.0.1",
        "port": int(cfg.get("port") or DEFAULT_PORTS[role]),
        "ngl": int(cfg.get("ngl") or 0),
        "ctx": int(cfg.get("ctx") or 4096),
    }
    path = resolve_model_path(role)
    if path:
        spec["model"] = path
    return spec


def resolve_model_path(role: str) -> str | None:
    """把角色里的**关键词**解析成 .gguf 绝对路径。"""
    from services.manager import discover_models
    keyword = str((load()["roles"].get(role) or {}).get("model") or "").strip()
    if not keyword:
        return None
    models = discover_models()
    for item in models:
        if keyword and keyword.lower() in str(item.get("file", "")).lower():
            return item.get("path")
    return None


# 端口探活的**短 TTL 缓存**（0.5 秒）：一次界面刷新里同一个端口会被问好几遍
# （`is_served` → `shared_with` → 再问别的角色），每问一次就是 0.35 秒。
# 0.5 秒足够短——服务刚起/刚停时最多滞后半秒，界面不会因为这个出错。
_ALIVE_TTL = 0.5
_ALIVE_CACHE: dict[int, tuple[float, bool]] = {}


def _alive(port: int, timeout: float = 0.35) -> bool:
    """端口上是不是有个能用的 llama-server（查 /health 最省事）。

    ⚠ **超时从 1.5 秒降到 0.35 秒**（2026-10-24）。原因实测：
      本机发往**关闭的**本地端口的连接**不会立即被拒**（而是被丢弃，跑满超时）
      —— `socket.connect(('127.0.0.1', 8099))` 实测 **1503 ms** 才 TimeoutError。
      于是 5 个角色 × 1.5 秒 = **7.7 秒**，全在 UI 主线程上 → 打开设置卡死。
      活的 llama-server 响应 `/health` 只要几毫秒，0.35 秒绰绰有余。
    """
    import time as _time
    hit = _ALIVE_CACHE.get(port)
    if hit and (_time.time() - hit[0]) < _ALIVE_TTL:
        return hit[1]

    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            ok = int(getattr(resp, "status", 200) or 200) < 500
            _ALIVE_CACHE[port] = (_time.time(), ok)
            return ok
    except urllib.error.HTTPError as exc:
        # 有些版本 /health 返回 503 表示"在加载"，那也算活着
        ok = exc.code in (200, 503)
        _ALIVE_CACHE[port] = (_time.time(), ok)
        return ok
    except Exception:
        _ALIVE_CACHE[port] = (_time.time(), False)
        return False


def running_roles() -> dict[str, bool]:
    """每个角色**能不能用**（共用的也算可用）。

    例：判官和思考模型是同一个 9B 文件、9B 已经在跑 →
        判官这一栏也报 True（界面不会显示"没跑"让人困惑）。
    """
    # **并发探活**：串行时 5 × 0.35 = 1.75 秒，并发只要 0.35 秒。
    # 而且 `is_served` 内部会走 `shared_with`（可能再探一次），所以并发更划算。
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(ROLES)) as pool:
        results = list(pool.map(is_served, ROLES))
    return dict(zip(ROLES, results))


def judge_endpoint() -> str | None:
    """**机会主义选判官**：按 `judge_prefer` 找第一个正在跑的，返回 chat/completions 地址。

    ⚠ 规则（用户定的）：判官只能是 think(9B) 或 judge(4B)，**memory(2B) 不许**。
      · 9B 在 → 用 9B（召回 100%），**不看 4B**
      · 9B 不在、4B 在 → 用 4B（召回 83%）
      · 都不在 → 返回 None，**调用方跳过判定**（不为默认关的门常驻 5 GB）
    """
    data = load()
    cfg = data["roles"]
    # ⚠ **机会主义开关**（用户定的规则）：
    #   开 → 有思考模型就用思考模型，没有就用「判官」那一行选定的模型
    #   关 → **只用「判官」那一行选的模型**，不管思考模型在不在
    if not switch("opportunistic_judge"):
        prefer = ["judge"]
    else:
        prefer = [r for r in (data.get("judge_prefer") or []) if r in JUDGE_OK]
    for role in prefer:
        # ⚠ 用 `effective_port`：判官和思考模型如果是同一个模型文件，
        #   直接连那个已经跑着的服务，**不另起一份**（防呆）。
        port = effective_port(role)
        if _alive(port):
            return f"http://127.0.0.1:{port}/v1/chat/completions"
    return None
