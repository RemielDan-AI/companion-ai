"""启动时自动拉起嵌入服务（8081）。

## 为什么需要

记忆检索是「BM25 词法 + bge 向量」混合，但向量侧要连本机 8081 的 `llama-server`
（`bge-small-zh-v1.5`，纯 CPU）。以前**得手动起**嵌入服务——
忘了开就一直退化在纯词法模式，而且**写入的记忆不会被向量化、也没有回填机制**
（`cognition/extract.py:297` 把 `add_texts` 的异常吞掉了），
于是索引会静默落后（实测：库里有 8 条，索引里只有 3 条）。

## 放在哪、不放在哪

**只放进程入口**（`gui.py` / `app.py` / `pet_server.py` 的 `main()`），
**不要放进 `Companion.__init__`**：测试会构造 Companion，那样会到处拉起 llama-server，
还会和自带起停嵌入服务的基准测试（`retrieval_bench_with_embed.py`）冲突。

## 取向

嵌入服务是**可选能力**——起不来只该退化成纯词法检索，不能让对话起不来。
所以本模块**永不抛异常**，只返回状态字典；调用方打印一行即可。

可用 `COMPANION_NO_EMBED_AUTOSTART=1` 关掉（跑基准测试时用）。
"""
from __future__ import annotations

import atexit
import os
import pathlib
import sys
import urllib.error
import urllib.request

from services.manager import EMBED_DEFAULTS, ServiceManager, health, port_free

# 我们拉起来的服务记在这里：退出时**只收自己起的**，
# 不去动别人手动开的那个（用户可能故意让它在别的窗口里跑）。
_STARTED_BY_US: dict[int, object] = {}



def _role_cfg(role: str) -> dict:
    """读 `config/models.yaml` 里某个角色；读不到就返回空 dict（**绝不抛**）。"""
    try:
        from services import models_config
        return (models_config.load().get("roles") or {}).get(role) or {}
    except Exception:
        return {}


def _role_model_path(role: str) -> str | None:
    try:
        from services import models_config
        return models_config.resolve_model_path(role)
    except Exception:
        return None


def _say(verbose: bool, msg: str) -> None:
    if verbose:
        try:
            print(msg, flush=True)
        except Exception:
            pass


def _embeddings_alive(host: str, port: int, timeout: int = 4) -> bool:
    """探测 /v1/embeddings 是否真的能用。

    为什么不用 `health()`：那个探 `/health`，只说明"有个 llama-server 在"，
    不保证带 `--embeddings`。起成 chat 模式的服务也会 /health 通过，
    但嵌入调用会 404/500。所以这里发一次真实的极小请求。
    """
    import json
    url = f"http://{host}:{port}/v1/embeddings"
    payload = json.dumps({"input": ["ping"], "model": "embedding"}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        return bool(body.get("data"))
    except Exception:
        return False


def ensure_embed_service(port: int | None = None, host: str | None = None,
                         keyword: str = "bge", wait: bool = True,
                         timeout: int = 120, verbose: bool = True) -> dict:
    """确保嵌入服务在跑。**永不抛异常。**

    返回 {ok, action, detail, pid}，action ∈
        already  本来就活着（不动它）
        started  这次是我们拉起来的（退出时会收掉）
        disabled 被 COMPANION_NO_EMBED_AUTOSTART 关掉
        foreign  端口被占，但不是能用的嵌入服务（不抢占）
        no_model 找不到 bge 模型文件
        no_exe   PATH 里没有 llama-server
        failed   启动失败
    """
    # ⚠ 2026-10-21：**默认值改成读 `config/models.yaml` 的 embed 角色**
    #   （界面「模型服务」页可改）。硬编码的 EMBED_DEFAULTS 只当兜底。
    spec = dict(EMBED_DEFAULTS)
    _cfg = _role_cfg("embed")
    port = int(port or _cfg.get("port") or spec["port"])
    host = str(host or spec["host"])
    if keyword == "bge":                      # 调用方没特意指定才用配置里的关键词
        keyword = str(_cfg.get("model") or keyword)
    spec["ngl"] = int(_cfg.get("ngl") or 0)
    spec["ctx"] = int(_cfg.get("ctx") or spec.get("ctx") or 512)

    # ① 已经能用 → 什么都不做（幂等，重复调用安全）
    if _embeddings_alive(host, port):
        return {"ok": True, "action": "already", "pid": None,
                "detail": f"{host}:{port} 已在提供嵌入"}

    # ② 用户明确关掉
    if os.environ.get("COMPANION_NO_EMBED_AUTOSTART") == "1":
        return {"ok": False, "action": "disabled", "pid": None,
                "detail": "COMPANION_NO_EMBED_AUTOSTART=1，跳过"}

    # ③ 端口被占但不是嵌入服务 → **不抢占**，可能用户自己起了别的东西
    if not port_free(host, port):
        return {"ok": False, "action": "foreign", "pid": None,
                "detail": f"端口 {port} 被占用，但 /v1/embeddings 不通；"
                          f"不抢占（先自己看看是谁）"}

    # ④ 找模型
    manager = ServiceManager()
    model = manager.find_model(keyword)
    if not model:
        # 退回：按角色配置里的关键词再试一次
        path = _role_model_path("embed")
        if path:
            model = {"path": path}
    if not model:
        return {"ok": False, "action": "no_model", "pid": None,
                "detail": f"没找到含「{keyword}」的模型（应在 C:\\ai\\models 下）"}

    spec.update({"model": model["path"], "kind": "embed", "port": port, "host": host})
    try:
        handle = manager.start(spec, wait=wait, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "action": "failed", "pid": None,
                "detail": f"{type(exc).__name__}: {exc}"}

    _STARTED_BY_US[port] = handle
    _register_cleanup()

    # 启动后**再验一次真调用**：`manager.start(wait=True)` 只探 `/health`，
    # 那只说明"有个 llama-server 在"，不保证带 `--embeddings`。
    # 万一起成了 chat 模式，进程会白占着 8081 却调不动嵌入 —— 那种状态比没起更糟。
    if not _embeddings_alive(host, port, timeout=8):
        try:
            manager.stop(port)
        except Exception:
            pass
        _STARTED_BY_US.pop(port, None)
        return {"ok": False, "action": "failed", "pid": None,
                "detail": "进程起来了但 /v1/embeddings 不通（可能没带 --embeddings），已收掉"}

    return {"ok": True, "action": "started", "pid": handle.get("pid"),
            "detail": f"已启动 {model['file']}（PID {handle.get('pid')}）"}


def ensure_extract_service(port: int | None = None, host: str | None = None,
                           keyword: str = "", wait: bool = True,
                           timeout: int = 180, verbose: bool = True,
                           manager: ServiceManager | None = None) -> dict:
    """确保**记忆抽取器**在跑（默认 8083 上的 Qwen3.5-2B）。**永不抛异常。**

    为什么单独一个服务：抽取和聊天共用一套权重会被微调带偏
    （实测官方基座抽「林小雨」正确、微调版抽成「林小remember」；
    详见 `cognition/extractor.py` 的说明与选型报告）。

    与 `ensure_embed_service` 同一个套路：
        ① 已通就什么都不做（幂等）　② 端口被占但不是目标服务 → 不抢占
        ③ 起不来只报告，抽取会**逐次回落到聊天模型**（不会让记忆不落库）
    """
    from services.manager import EXTRACT_DEFAULTS, EXTRACT_MODEL_KEYWORD

    # ⚠ 2026-10-21：**模型/端口/ngl 从 `config/models.yaml` 的 memory 角色读**
    #   （界面「模型服务」页可改）。硬编码的 EXTRACT_DEFAULTS 只当兜底。
    spec = dict(EXTRACT_DEFAULTS)
    _cfg = _role_cfg("memory")
    port = int(port or _cfg.get("port") or spec["port"])
    host = str(host or spec["host"])
    keyword = keyword or str(_cfg.get("model") or EXTRACT_MODEL_KEYWORD)
    spec["ngl"] = int(_cfg.get("ngl") if _cfg.get("ngl") is not None else spec["ngl"])
    spec["ctx"] = int(_cfg.get("ctx") or spec.get("ctx") or 4096)

    # ① 已经在提供 chat completions → 不动它
    if health(port, host, timeout=3).get("ok"):
        return {"ok": True, "action": "already", "pid": None,
                "detail": f"{host}:{port} 已在服务"}

    # ② 明确关掉（跑基准/对照实验时用）
    if os.environ.get("COMPANION_NO_EXTRACT_AUTOSTART") == "1":
        return {"ok": False, "action": "disabled", "pid": None,
                "detail": "COMPANION_NO_EXTRACT_AUTOSTART=1，跳过"}

    # ③ 端口被占但不是 chat → 不抢占
    if not port_free(host, port):
        return {"ok": False, "action": "foreign", "pid": None,
                "detail": f"端口 {port} 被占用但不是可用服务；不抢占"}

    # ④ 找模型
    manager = manager or ServiceManager()
    model = manager.find_model(keyword)
    if not model:
        return {"ok": False, "action": "no_model", "pid": None,
                "detail": f"没找到含「{keyword}」的模型（应在 C:\\ai\\models 下）"}

    spec.update({"model": model["path"], "kind": "chat", "port": port,
                 "host": host})
    try:
        handle = manager.start(spec, wait=wait, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "action": "failed", "pid": None,
                "detail": f"{type(exc).__name__}: {exc}"}

    _STARTED_BY_US[port] = handle
    _register_cleanup()
    return {"ok": True, "action": "started", "pid": handle.get("pid"),
            "detail": f"已启动 {model['file']}（PID {handle.get('pid')}）"}



def ensure_chat_service(port: int | None = None, host: str | None = None,
                        keyword: str = "", wait: bool = True,
                        timeout: int = 180, verbose: bool = True,
                        manager: ServiceManager | None = None) -> dict:
    """确保**对话模型**在跑（默认 8080）。**永不抛异常。**

    ⚠ 2026-10-21 新增（界面「一键拉起」要用）。原来对话模型只由
      `launcher.py` / 启动模型.bat 拉，界面里没法一键起。

    参数（模型/端口/ngl/ctx）**从 `config/models.yaml` 的 chat 角色读**。

    返回 {ok, action, detail, pid}，action ∈
        already / started / disabled / foreign / no_model / no_exe / failed
    """
    from services.manager import CHAT_DEFAULTS

    spec = dict(CHAT_DEFAULTS)
    _cfg = _role_cfg("chat")
    port = int(port or _cfg.get("port") or spec["port"])
    host = str(host or spec["host"])
    keyword = keyword or str(_cfg.get("model") or "")
    spec["ngl"] = int(_cfg.get("ngl") if _cfg.get("ngl") is not None else spec["ngl"])
    spec["ctx"] = int(_cfg.get("ctx") or spec.get("ctx") or 4096)

    # ① 已经在提供 chat completions → 不动它（幂等）
    if health(port, host, timeout=3).get("ok"):
        return {"ok": True, "action": "already", "pid": None,
                "detail": f"{host}:{port} 已在服务"}

    # ② 明确关掉
    if os.environ.get("COMPANION_NO_CHAT_AUTOSTART") == "1":
        return {"ok": False, "action": "disabled", "pid": None,
                "detail": "COMPANION_NO_CHAT_AUTOSTART=1，跳过"}

    # ③ 端口被占但不是 chat → **不抢占**（可能用户自己起了别的）
    if not port_free(host, port):
        return {"ok": False, "action": "foreign", "pid": None,
                "detail": f"端口 {port} 被占用，但不是可用的对话服务；不抢占"}

    # ④ 找模型：先按角色配置解析，再退回关键词
    manager = manager or ServiceManager()
    model = None
    path = _role_model_path("chat")
    if path:
        model = {"path": path}
    if model is None and keyword:
        model = manager.find_model(keyword)
    if not model:
        return {"ok": False, "action": "no_model", "pid": None,
                "detail": f"没找到对话模型（角色配置：{keyword or '空'}）"}

    spec.update({"model": model["path"], "kind": "chat",
                 "port": port, "host": host})
    try:
        handle = manager.start(spec, wait=wait, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "action": "failed", "pid": None,
                "detail": f"{type(exc).__name__}: {exc}"}

    _STARTED_BY_US[port] = handle
    _register_cleanup()
    if not health(port, host, timeout=8).get("ok"):
        try:
            manager.stop(port)
        except Exception:
            pass
        _STARTED_BY_US.pop(port, None)
        return {"ok": False, "action": "failed", "pid": None,
                "detail": f"起了但 {host}:{port} 没响应（看 logs/）"}
    return {"ok": True, "action": "started", "pid": getattr(handle, "pid", None),
            "detail": f"已拉起 {host}:{port}（{pathlib.Path(model['path']).name}）"}



def stop_started(verbose: bool = True) -> list[int]:
    """停掉**本次拉起的**服务，不动用户手动开的那些。

    ## 为什么必须单独有这么个函数

    `ServiceManager.stop_all()` 只停**它自己那个实例**记着的服务。
    而 `ensure_*_service` 每次调用会**新建一个 `ServiceManager`**，
    所以界面持有的那个 manager **根本不知道** autostart 起过什么——
    关闭窗口时调 `stop_all()` 收不走它们，会留孤儿进程。

    这里收的是本模块的 `_STARTED_BY_US`（只在 `ensure_*` 成功时写入），
    所以**手动开的服务不会被误杀**。
    """
    stopped: list[int] = []
    for port, handle in list(_STARTED_BY_US.items()):
        proc = (handle or {}).get("proc") if isinstance(handle, dict) else None
        ok = False
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except Exception:
                    proc.kill()
                ok = True
            else:
                ok = True          # 早就退了，也算收干净
        except Exception:
            try:
                proc.kill()        # type: ignore[union-attr]
                ok = True
            except Exception:
                ok = False
        _STARTED_BY_US.pop(port, None)
        if ok:
            stopped.append(port)
    if verbose and stopped:
        _say(True, f"已停掉 {len(stopped)} 个由本程序拉起的服务：{stopped}")
    return stopped


def start_think(verbose: bool = True) -> dict:
    """拉起**思考模型**（think 角色）；按开关先关掉**判官模型**腾内存。

    ## 为什么两个要互斥

    实测（本机 15.19 GB / 4 GB 显存）：
        三个常驻（对话+嵌入+记忆）           剩 5.66 GB
        ＋判官 4B（2.6 GB）                 剩 1.22 GB
        ＋思考 9B（5.2 GB）                 剩 **0.44 GB**
        五个全上                            剩 **0.03 GB**（会换页抖动，等于不能用）

    而**它们从不同时需要**：思考模型只在难题时用，判官只在生成完判接地时用。
    所以让后来者把先来者顶掉是划算的——`exclusive_think` 开关控制这条。

    返回 {ok, action, detail, stopped}。
    """
    from services import models_config

    stopped: list[int] = []
    if models_config.switch("exclusive_think"):
        try:
            judge_port = int(models_config.load()["roles"]["judge"].get("port") or 8084)
        except Exception:
            judge_port = 8084
        # 只在"判官**是我们起的**"时才关它；用户手动开的不动
        if judge_port in _STARTED_BY_US:
            mgr = ServiceManager()
            try:
                mgr.stop(judge_port)
                stopped.append(judge_port)
                _STARTED_BY_US.pop(judge_port, None)
                if verbose:
                    _say(True, f"已先关掉判官模型（端口 {judge_port}）给思考模型腾内存")
            except Exception:
                pass
        elif not port_free("127.0.0.1", judge_port):
            # 判官是手动开的 → 不抢，但要如实告诉用户内存可能不够
            if verbose:
                _say(True, f"⚠ 端口 {judge_port} 上的判官不是你这边起的，不动它；"
                            f"两个同时开会很吃内存")

    # 已经活着就用它
    try:
        think_port = int(models_config.load()["roles"]["think"].get("port") or 8085)
    except Exception:
        think_port = 8085
    if health(think_port, "127.0.0.1", timeout=3).get("ok"):
        return {"ok": True, "action": "already", "stopped": stopped,
                "detail": f"思考模型已在 {think_port}"}

    if os.environ.get("COMPANION_NO_THINK_AUTOSTART") == "1":
        return {"ok": False, "action": "disabled", "stopped": stopped,
                "detail": "COMPANION_NO_THINK_AUTOSTART=1，跳过"}

    path = _role_model_path("think")
    if not path:
        return {"ok": False, "action": "no_model", "stopped": stopped,
                "detail": "思考模型没配或找不到文件（看 config/models.yaml 的 think）"}

    from services.manager import CHAT_DEFAULTS
    spec = dict(CHAT_DEFAULTS)
    _cfg = _role_cfg("think")
    spec.update({
        "model": path,
        "kind": "chat",
        "port": think_port,
        "ngl": int(_cfg.get("ngl") if _cfg.get("ngl") is not None else 20),
        "ctx": int(_cfg.get("ctx") or 4096),
    })
    mgr = ServiceManager()
    try:
        handle = mgr.start(spec, wait=True, timeout=240)
    except Exception as exc:
        return {"ok": False, "action": "failed", "stopped": stopped,
                "detail": f"{type(exc).__name__}: {exc}"}
    _STARTED_BY_US[think_port] = handle
    _register_cleanup()
    if not health(think_port, "127.0.0.1", timeout=10).get("ok"):
        try:
            mgr.stop(think_port)
        except Exception:
            pass
        _STARTED_BY_US.pop(think_port, None)
        return {"ok": False, "action": "failed", "stopped": stopped,
                "detail": f"起来了但 {think_port} 没响应（看 logs/）"}
    return {"ok": True, "action": "started", "stopped": stopped,
            "detail": f"思考模型已拉起（{pathlib.Path(path).name}）"}


def _register_cleanup() -> None:
    """退出时收掉**我们起的**服务（嵌入 / 抽取），避免留孤儿。

    `ServiceManager` 会把子进程绑进 KILL_ON_JOB_CLOSE 的 job object，
    父进程正常退出时本来就会收走；但 `AssignProcessToJobObject` 的失败被
    `except` 吞掉了（嵌套 job 等情况下可能失败），所以这里再兜一层。
    只收 `_STARTED_BY_US` 里记着的，绝不动手动开的那个。
    """
    if getattr(_register_cleanup, "_done", False):
        return
    _register_cleanup._done = True       # type: ignore[attr-defined]

    def _bye() -> None:
        for p, handle in list(_STARTED_BY_US.items()):
            proc = (handle or {}).get("proc")
            try:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    try:
        atexit.register(_bye)
    except Exception:
        pass


def report(result: dict, verbose: bool = True, label: str = "记忆检索") -> None:
    """把 ensure_*_service 的结果打成一行，供入口调用。"""
    action = result.get("action")
    if action == "started":
        _say(verbose, f"[{label}] 已自动拉起：{result['detail']}")
    elif action == "already":
        _say(verbose, f"[{label}] 服务已在跑（{result['detail']}）")
    elif action == "disabled":
        _say(verbose, f"[{label}] 自动拉起已关闭")
    else:
        _say(verbose, f"[{label}] 没起来（{action}）：{result.get('detail')}"
                      "　→ 自动退化为可用的降级路径，对话不受影响")


def main() -> int:
    """手动试一次：python -m services.autostart"""
    tmp = dict(EMBED_DEFAULTS)
    result = ensure_embed_service(verbose=True)
    report(result)
    _say(True, f"详情：{result}")
    _say(True, f"提示：设置 COMPANION_NO_EMBED_AUTOSTART=1 可跳过自动拉起"
               f"（端口 {tmp['port']}）")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
