"""记忆抽取器的客户端：**独立模型 + 失败回落**。

## 为什么要独立

抽取和聊天**共用一套权重**会把抽取带偏：微调数据里没有抽取样本
（`weiyang_train.jsonl` 1085 条含抽取特征 **0 条**），于是微调后名字被抽坏
（实测「林小雨」→「**林小记得**」；官方基座同题是 `林小雨（妹妹）`）。

对照实测（`tests/analysis/multihop_e2e.py`，3 题 × 3 次，真实抽取链）：

    抽取器              体积      真实链路多跳
    **Qwen3.5-2B**     1.19GB    **13/18 = 72%**   ← 采用
    官方 Qwen3-4B       2.53GB     6/9  = 67%
    Qwen2.5-1.5B        1.04GB     5/9  = 56%
    微调 v1.3（共权重）  2.33GB     3/9  = 33%
    R1-Distill-1.5B     1.04GB     2/9  = 22%      ← 会抄提示词模板

## 取向

抽取服务是**可选增强**——起不来必须能退回聊天模型，绝不能因为抽取器不可用
就让记忆不落库。所以 `Extractor.complete()` **逐次尝试、失败即回落**，
而不是启动时一次性判断（服务可能中途挂掉）。
"""
from __future__ import annotations

import os

DEFAULT_EXTRACT_URL = "http://127.0.0.1:8083/v1/chat/completions"


class Extractor:
    """把「抽取用模型」和「聊天模型」包成一个 `complete()`。

    用法与 `Generator` 一致（抽取只用到 `complete`）：
        ex = Extractor(primary=extract_gen, fallback=chat_gen)
        raw = ex.complete(EXTRACT_SYSTEM, prompt, max_tokens=400, temperature=0.2)
    """

    def __init__(self, primary, fallback=None, verbose: bool = True):
        self.primary = primary
        self.fallback = fallback
        self.verbose = verbose
        self.last_used = "primary" if primary is not None else "fallback"
        self.fallback_count = 0
        self._notified = False

    def complete(self, *args, **kwargs) -> str:
        if self.primary is not None:
            try:
                out = self.primary.complete(*args, **kwargs)
                self.last_used = "primary"
                return out
            except Exception as exc:
                # 抽取器挂了不能连带记忆一起丢——回落到聊天模型，并且只提示一次
                self.last_used = "fallback"
                self.fallback_count += 1
                if self.verbose and not self._notified:
                    self._notified = True
                    try:
                        print(f"[记忆抽取] 抽取器不可用（{type(exc).__name__}），"
                              f"本轮起回落到聊天模型；记忆照常落库", flush=True)
                    except Exception:
                        pass
        if self.fallback is not None:
            return self.fallback.complete(*args, **kwargs)
        raise RuntimeError("抽取器与聊天模型都不可用")

    # 抽取路径只用到 complete；其余属性转发给 primary，便于排查
    def __getattr__(self, name):
        primary = self.__dict__.get("primary")
        if primary is not None:
            return getattr(primary, name)
        raise AttributeError(name)


def extract_url(explicit: str | None = None) -> str:
    """记忆抽取该连的地址。

    ⚠ 2026-10-21：**走 `models_config.endpoint("memory")`**——
      用户可能把「记忆」和「对话」配成同一个模型（大模型两样都能干），
      这时只有一个服务在跑，抽取必须把活路由到它，而不是写死 8083。
      优先级：显式参数 → 环境变量 → 角色配置 → 内置默认。
    """
    if explicit or os.environ.get("COMPANION_EXTRACT_URL"):
        return (explicit or os.environ.get("COMPANION_EXTRACT_URL")
                or DEFAULT_EXTRACT_URL)
    try:
        from services import models_config
        return models_config.endpoint("memory")
    except Exception:
        return DEFAULT_EXTRACT_URL
