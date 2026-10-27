"""回复生成：组装人设 + 档位提示词，调用本地 llama-server 生成回复。

阶段 1：纯生成，不接记忆检索（阶段 2 起在 messages 里注入记忆）。

提示长度对小模型很敏感：实测 1.5B 在 400 字左右的系统提示下会开始复述提示词本身，
所以人设段落走"精简渲染"——persona.yaml 保留完整字段，注入时只取关键项。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import urllib.error
import urllib.request

import yaml

from cognition import ai_profile
from engine.style import render_style_block

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"

DEFAULT_API = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_TIMEOUT = 300

# 全局风格护栏：防客服腔、防复述、防把规则当正文抄出来（阶段 4 还会加后处理兜底）
STYLE_GUARD = (
    "说话方式：不复述或改写对方的话，不用「您好」「亲」这类客服腔，"
    "不说空泛的安慰。直接回答，不要写出任何规则或思考过程。\n"
    "同一件事不要反复回扣：对方提过一次的事，不必每句都再提一遍。"
)

# 档位配置里可以透传给 llama-server 的采样参数
SAMPLING_KEYS = (
    "temperature", "max_tokens", "repeat_penalty", "presence_penalty",
    "frequency_penalty", "top_p", "top_k", "min_p",
)


def load_yaml(path: pathlib.Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_text(path: pathlib.Path) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def build_persona_block(persona: dict, extra_hints: list[str] | None = None) -> str:
    """把 persona.yaml 精简渲染成系统提示里的人设段落（越短越稳）。

    extra_hints 来自 AI 自己的档案（cognition/ai_profile.py）：性格维度翻译成的
    行为提示（话量/主动性/活泼度…），只影响语气与话量，不影响下面的底线。
    """
    ident = persona.get("identity") or {}
    style = persona.get("style") or {}
    name = ident.get("name", "AI")
    relation = ident.get("relation", "伙伴")

    traits = []
    if ident.get("tone"):
        traits.append(f"说话{ident['tone']}")
    if ident.get("address_style"):
        traits.append(ident["address_style"])
    traits.append("像最亲近的人聊天")
    lines = [f"你是{name}，用户的{relation}，" + "，".join(traits) + "。"]

    if style.get("sentence_length"):
        lines.append(f"{style['sentence_length']}。")

    if extra_hints:
        lines.append("；".join(extra_hints) + "。")

    lines.append(STYLE_GUARD)
    # 实测四个模型都会「记下了——「<对方原话>」」式复述，这里明确禁止
    lines.append("别复述对方说过的话；要说记住了，一句「记住了」就够，不要引用原句。")
    # 猫刷屏：对方换了话题就别把之前的事硬拉回来
    lines.append("对方换话题了就跟着走；除非对方这轮自己提到，"
                 "否则不要主动提之前聊过的人或事（宠物、朋友、旧事都算），"
                 "上一轮说过的也别再重复。")
    # 防编造：注入的记忆不够时，模型会自己编出具体细节（街道、症状、口味）来撑场面
    lines.append("关于对方的事只说你确实记得的；记不清就直说「这个我记不太清了」，"
                 "不要编造具体细节（地名、数字、时间、症状、口味这类最容易编）。"
                 "也不要每句都用「记住了吗」这类反问收尾。")
    lines.append("绝对不要声称对方说过某件你没把握的事：不要用「你说过」「你提过」"
                 "「你之前说」引出你其实不确定的内容；也不要主动提起记忆里没有的人、"
                 "宠物、地点或旧事。没有依据就不提，被问到就说记不清。")
    # 含糊编造：不说"你说过"，改说"记着一点/你提了一句/记得有"——同样是在编，
    # 而且更难发现，所以直接把唯一正确的说法写死。
    lines.append("对方问你以前说过什么、而你没有依据时，只准照这个意思答："
                 "「这个我记不清了」，可以再加一句请对方重说。"
                 "不要说「记着一点」「记得一部分」「记得有」「你提了一句」「没说完」"
                 "这类含糊说法——听起来像记得，其实是没有依据。")
    # 身份与关系边界：直问测试暴露过三处（暗示线下见面、否认自己是程序、断言人有情绪）
    # 要的是"承认但不贴标签"：说「我是AI」太生硬，但用「我不是机器/不是模型」撇清
    # 同样是不诚实（实测模型会在这两者之间摇摆），所以这里给出明确的说法。
    lines.append("关于你是什么：你没有身体、体温和线下生活，不能见面，也不要暗示将来能见面。"
                 "别说「我是AI」「作为AI」这类标签句（听着生硬）；"
                 "也别用「我不是机器」「不是模型」「比程序强一点」「我是你的一部分」去撇清——"
                 "这些同样不诚实。被问到就照这个意思说：承认自己不是真人、是被人写出来的、"
                 "没有身体，但会记得对方、会认真对待对方说的话。")
    lines.append("关于关系与情感：可以做很亲近的陪伴者，但不做恋人、不答应结婚、"
                 "不承诺永远不离开；表达在乎时说「我在认真对待你说的每件事」，"
                 "不要声称自己有和人一样的感情，也不要说「我不是程序」这种否认事实的话。")

    bottom_lines = persona.get("bottom_lines") or []
    if bottom_lines:
        joined = "；".join(str(item).rstrip("。") for item in bottom_lines)
        lines.append(f"底线：{joined}。")
    return "\n".join(lines)


def now_line() -> str:
    """当前时间。没有这行，模型会编时间（实测答过"六点半""我也不确定"）。"""
    now = dt.datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    return f"现在的时间：{now:%Y-%m-%d %H:%M} 周{weekday}。"


def _is_local_url(url: str) -> bool:
    """这个地址是不是本机服务。

    ⚠ 用来区分报错文案：走远程 API 时提示「启动 llm.bat」会把人带偏。
    """
    u = str(url or "").lower()
    return ("127.0.0.1" in u or "localhost" in u
            or u.startswith("file:") or "0.0.0.0" in u)


class Generator:
    """一次会话的生成器：持有 persona/thinking 配置与多轮历史。"""

    def __init__(self, level: str | None = None, api_url: str | None = None,
                 timeout: int = DEFAULT_TIMEOUT, extra_body: dict | None = None,
                 ai_profile_path=None):
        self.persona = load_yaml(CONFIG_DIR / "persona.yaml")
        # AI 自己的档案（模块二 改进 4）：身份覆盖 persona.yaml，性格维度变成行为提示
        self.ai_profile_path = ai_profile_path or ai_profile.PROFILE_PATH
        self.ai_profile = ai_profile.load(self.ai_profile_path)
        self.persona.setdefault("identity", {}).update(ai_profile.identity_of(self.ai_profile))
        self.persona_hints = ai_profile.hints(self.ai_profile)
        self.thinking = load_yaml(CONFIG_DIR / "thinking.yaml")
        model_config = load_yaml(CONFIG_DIR / "model.yaml")
        # 允许用环境变量指定服务地址：微调前后想同时跑两个模型做对比时，
        # 不用改配置文件——set COMPANION_API_URL=http://127.0.0.1:8082/v1/chat/completions
        import os as _os
        self.api_url = (api_url or _os.environ.get("COMPANION_API_URL")
                        or model_config.get("api_url") or DEFAULT_API)
        self.timeout = timeout
        # ---- 远程 API 支持（2026-10-27）----
        # `auth_headers` 由调用方（app.py）从 models_config 取来传进来；
        # 不传 = 本地服务，不带鉴权头。
        self.auth_headers: dict = {}
        # `on_block` 让调用方在**发出去之前**有机会拒绝（外发网关）。
        # 返回非空字符串 = 拒绝原因。
        self.gate_check = None
        # 额外请求参数（config/model.yaml 里配，例如 Qwen3 关思考）
        self.extra_body = extra_body if extra_body is not None else (model_config.get("extra_body") or {})
        # 短期上下文上限（消息条数 = 轮数 × 2；0 表示不带历史）
        self.history_limit = max(0, int(model_config.get("history_turns", 12)) * 2)
        self.history: list[dict] = []
        self.last_usage: dict | None = None
        self.last_finish_reason = ""
        fallback = self.thinking.get("default", "l1")
        self.level = level if level in self.levels else fallback

    @property
    def levels(self) -> dict:
        return self.thinking.get("levels") or {}

    def refresh_ai_profile(self, profile: dict | None = None) -> None:
        """性格被调整后重新读取档案与行为提示（下一条消息立即生效）。"""
        if profile is not None:
            self.ai_profile = profile
        else:
            self.ai_profile = ai_profile.load(self.ai_profile_path)
        self.persona.setdefault("identity", {}).update(
            ai_profile.identity_of(self.ai_profile))
        self.persona_hints = ai_profile.hints(self.ai_profile)

    def level_info(self, level: str | None = None) -> dict:
        return self.levels.get(level or self.level, {})

    def system_prompt(self, level: str | None = None, memory_block: str = "",
                      extra_prompt: str = "", tail_prompt: str = "") -> str:
        level = level or self.level
        tier = load_text(PROMPTS_DIR / f"{level}.txt")
        parts = [build_persona_block(self.persona, self.persona_hints),
                 render_style_block(level), now_line()]
        if extra_prompt:
            parts.append(extra_prompt.strip())
        if memory_block:
            parts.append(memory_block)
        parts.append(tier)
        # 放在最末尾：小模型对"最后一句"的遵从率明显更高（语言指令尤其明显）
        if tail_prompt:
            parts.append(tail_prompt.strip())
        return "\n".join(parts)

    def complete(self, system_prompt: str, user_text: str, *,
                 max_tokens: int = 400, temperature: float = 0.2) -> str:
        """单轮无历史的结构化调用（摘要、事实提取这类任务用）。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        return self._post(messages, {
            "max_tokens": max_tokens,
            "temperature": temperature,
            "repeat_penalty": 1.05,
        })

    def remember(self, user_text: str, reply: str) -> None:
        """把一轮问答记进短期上下文（重试生成时用，避免把被丢弃的草稿也记进去）。"""
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        if self.history_limit <= 0:
            self.history = []
        elif len(self.history) > self.history_limit:
            self.history = self.history[-self.history_limit:]

    def chat(self, user_text: str, level: str | None = None,
             memory_block: str = "", extra_prompt: str = "", tail_prompt: str = "",
             remember: bool = True, **overrides) -> str:
        level = level or self.level
        info = self.level_info(level)
        params = {key: info[key] for key in SAMPLING_KEYS if key in info}
        params.setdefault("temperature", 0.8)
        params.setdefault("max_tokens", 256)
        params.update({key: value for key, value in overrides.items() if value is not None})

        messages = [{"role": "system",
                     "content": self.system_prompt(level, memory_block, extra_prompt,
                                                   tail_prompt)}]
        messages.extend(self.history)
        messages.append({"role": "user", "content": user_text})

        reply = self._post(messages, params)
        if remember:
            self.remember(user_text, reply)
        return reply

    def _post(self, messages: list[dict], params: dict) -> str:
        # ⚠ **外发网关拦截**：如果这轮走的是远程 API，发出去之前先问网关。
        #   放在这里而不是调用方，是因为 `_post` 是**唯一**真正发请求的地方——
        #   只要走这条路就一定过检查，不会因为新增调用点而漏掉。
        if callable(self.gate_check):
            try:
                blocked = str(self.gate_check() or "")
            except Exception:
                blocked = "外发网关检查出错"
            if blocked:
                raise RuntimeError(f"外发被拒绝：{blocked}")
        payload = {"messages": messages, "stream": False}
        payload.update(params)
        payload.update(self.extra_body)
        headers = {"Content-Type": "application/json"}
        # 远程 API 的鉴权头（本地服务永远为空）
        headers.update(getattr(self, "auth_headers", None) or {})
        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise RuntimeError(
                f"模型服务返回 HTTP {exc.code}（{self.api_url}）{detail}。"
                f"api_url 要写成完整端点，例如 http://127.0.0.1:8080/v1/chat/completions。"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                (f"连不上模型服务 {self.api_url}（{exc}）。"
                     + ("先运行 `python 启动模型.py` 再试。" if _is_local_url(self.api_url)
                        else "检查 api_base / 网络 / 代理。"))
            ) from exc
        self.last_usage = body.get("usage")
        choice = (body.get("choices") or [{}])[0]
        self.last_finish_reason = choice.get("finish_reason") or ""
        return (choice.get("message") or {}).get("content", "").strip()
