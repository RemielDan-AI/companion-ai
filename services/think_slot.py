"""思考模型的**按需起停**：难题来了才起，空闲一段时间自动关。

## 为什么要有这一层

9B 常驻太贵：三个常驻已占 5.66 GB，再加 9B 只剩 **0.44 GB**（实测），
五个全上只剩 **0.03 GB**（会换页，等于不能用）。
而它**只在难题时有用**——所以按需起、空闲关，是唯一能接受的做法。

## 两个**独立**的开关（别混）

    ① `auto_think.enabled`  难题来了**自动拉起**（省得手动点按钮）
    ② `auto_think.mode`     拉起之后**要不要让难题真的交给人它答**

⚠ 为什么分开：**9B 是未微调的原版**——任务能力强，但人设/风格弱。
  实测（纯净测试 18 变体 × 20 轮）：

      weiyang-v1.5        自我约束 43/60   平均 29.5 字
      Qwen3.5-9B（原版）   自我约束 **28/60**  平均 ~110 字

  也就是：**难题交给它，答得更对，但说话不像未央了。**
  所以 `mode` 默认 `judge_only`（只当判官，已经接好、零风险），
  想让它直接答题再改成 `generate`，并且**得接受风格回退**。

## 起模型要多久

实测 9B 加载 **10~15 秒**（ngl=20，显存占一部分）。这个等待在**提问的那一轮**
上，用户会真的等。所以 `enabled` 默认 **false**——这是有代价的功能，不该默认开。
"""
from __future__ import annotations

import threading
import time

from services import autostart, models_config

LEVELS = ("l0", "l1", "l2", "l3")

# 上一次"需要思考模型"的时间戳（秒）；0 = 从没需要过
_last_need: float = 0.0
_lock = threading.Lock()
_idle_thread: threading.Thread | None = None


def _config() -> dict:
    try:
        return dict((models_config.load() or {}).get("auto_think") or {})
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_config().get("enabled", False))


def mode() -> str:
    raw = _config().get("mode")
    # ⚠ **YAML 陷阱**：`mode: off` 会被 PyYAML 解析成**布尔 False**
    #   （YAML 1.1 里 on/off/yes/no 都是布尔）。所以这里两个都要认，
    #   否则 `off` 会掉进"写错了 → 退回 judge_only"的分支，**关了等于没关**。
    if raw is False:
        return "off"
    m = str(raw or "judge_only").strip().lower()
    # transfer = **9B 出内容、v1.5 转述成自己的口吻**（用户 2026-10-21 定的方案）
    #   这是三者里最省风格的：内容由强的模型给，声音由微调过的模型出。
    return m if m in ("judge_only", "generate", "transfer", "off") else "judge_only"


def min_level() -> str:
    lv = str(_config().get("min_level") or "l2").strip().lower()
    return lv if lv in LEVELS else "l2"


def idle_minutes() -> float:
    try:
        return max(1.0, float(_config().get("idle_minutes") or 10))
    except Exception:
        return 10.0


def _think_port() -> int:
    try:
        return int((models_config.load()["roles"]["think"]).get("port") or 8085)
    except Exception:
        return 8085


def url() -> str:
    """思考模型该连的地址——**走 `effective_port`**。

    ⚠ 如果思考模型和别的角色是同一个文件、那个角色的服务已经在跑，
      这里必须指向**它**，否则会去连一个空端口（起不来就永远用不上）。
    """
    try:
        return models_config.endpoint("think")
    except Exception:
        return f"http://127.0.0.1:{_think_port()}/v1/chat/completions"


def is_up() -> bool:
    try:
        return bool(models_config.running_roles().get("think"))
    except Exception:
        return False


def should_trigger(level: str) -> bool:
    """这一档难度够不够触发。

    ⚠ 起模型要 10~15 秒，所以**只在真的深档**触发（默认 l2 起）——
      l0/l1 的日常闲聊绝不能为它等十几秒。
    """
    if not enabled() or mode() == "off":
        return False
    if level not in LEVELS:
        return False
    return LEVELS.index(level) >= LEVELS.index(min_level())


def touch() -> None:
    """记一次"刚用过"，供空闲回收判断。"""
    global _last_need
    with _lock:
        _last_need = time.time()


def need(level: str, verbose: bool = False) -> bool:
    """难题来了：该起就起。返回**这一轮能不能用上思考模型**。

    · 已经在跑 → 直接 True（顺便 touch 一下，别让它被回收）
    · 没在跑、档位够 → 起（**阻塞 10~15 秒**），起来后 True
    · 档位不够 / 开关关 → False
    """
    if not should_trigger(level):
        return False
    touch()
    if is_up():
        return True
    try:
        result = autostart.start_think(verbose=verbose)
    except Exception:
        return False
    if result.get("ok"):
        _ensure_idle_watch()
        return True
    return False


def stop(verbose: bool = False) -> bool:
    """关掉思考模型（**只关我们起的**）。"""
    port = _think_port()
    try:
        if port not in getattr(autostart, "_STARTED_BY_US", {}):
            # 不是我们起的（用户手动开的）→ 不动
            return False
        from services.manager import ServiceManager
        ServiceManager().stop(port)
        autostart._STARTED_BY_US.pop(port, None)
        if verbose:
            print(f"[思考模型] 空闲 {idle_minutes():.0f} 分钟，已自动关闭（端口 {port}）")
        return True
    except Exception:
        return False


def _idle_tick() -> None:
    """后台线程：空闲超时就收掉。"""
    global _last_need
    while True:
        time.sleep(30)
        try:
            if not _last_need or not is_up():
                continue
            idle_sec = time.time() - _last_need
            if idle_sec >= idle_minutes() * 60:
                stop(verbose=True)
                with _lock:
                    _last_need = 0.0
        except Exception:
            continue


def _ensure_idle_watch() -> None:
    """起一个守护线程盯空闲（只起一次）。"""
    global _idle_thread
    if _idle_thread is not None and _idle_thread.is_alive():
        return
    _idle_thread = threading.Thread(target=_idle_tick, name="think-idle", daemon=True)
    _idle_thread.start()


def status() -> dict:
    """给界面/内情行用的一行状态。"""
    return {
        "enabled": enabled(),
        "mode": mode(),
        "min_level": min_level(),
        "idle_minutes": idle_minutes(),
        "running": is_up(),
        "url": url(),
    }
