#!/usr/bin/env python
"""陪伴 AI 主入口 —— 阶段 2：带记忆的对话。

每轮流程：检索记忆 → 注入系统提示 → 生成回复 → 提取认知 → 写记忆与画像。

用法：
    python app.py                    # 交互式对话（默认 L1 轻思，带记忆）
    python app.py --level l2         # 指定起始档位
    python app.py --no-memory        # 关掉记忆（对照用，等价阶段 1 行为）
    python app.py --test             # 内置 5 轮测试并存样例
    python app.py --stats            # 只看记忆库/画像现状

对话中可用的命令：
    /l0 /l1 /l2 /l3        切换思考档位
    /reset                 清空短期上下文（不动长期记忆）
    /memory                看记忆库现状
    /recall 关键词          手动检索，看命中哪些记忆
    /profile               看个人画像
    /wipe                  清除长期记忆（调试用，需二次确认）
    /good [备注]           给上一轮点赞
    /bad [备注]            给上一轮点踩
    /fix 说明              纠正上一轮（同时记为反馈）
    /save                  把本次对话存成样例
    /quit                  退出
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

from cognition.extract import PROFILE_PATH, load_profile, remember_turn
from cognition.memory import Memory
from cognition import values as values_engine
from cognition import ai_profile
from cognition.vectors import VectorIndex, health as embedding_health
from engine import emotion as emotion_engine
from engine import refuse
from cognition.verify import numbers_in, unsupported_numbers  # noqa: E402
from engine.generate import now_line  # noqa: E402
from engine import style as style_engine
from engine.generate import Generator
from engine.retrieve import build_memory_block, retrieve
from feedback import collector
from router.depth_router import LEVEL_NAMES, route_depth
from router.skill_router import SkillRouter
from skills import executors
from tools.tool_loop import (build_runner, collect_tools, needs_model_tools,
                             run_loop)
from tools.protocol import tools_prompt

LEVELS = ["l0", "l1", "l2", "l3"]

ROOT = pathlib.Path(__file__).resolve().parent
SAMPLES_DIR = ROOT / "samples"
BACKUP_DIR = ROOT / "backups"

# 内置测试：覆盖情绪陪伴、快答、需要认真想的事，带追问检验上下文衔接
TEST_DIALOGUE = [
    ("l1", "今天有点累，什么都不想干。"),
    ("l1", "嗯，就是提不起劲，坐那儿发呆了一下午。"),
    ("l0", "晚上吃点什么好？"),
    ("l2", "我最近一直在想要不要换工作，但总下不了决心。"),
    ("l2", "主要是怕换过去还不如现在。"),
]



# ---------------- 两段式补充的判定工具 ----------------
# ⚠⚠ **停用字表只许放纯虚词**（2026-10-20 修的真 bug）。
#   第一版把 `说/想/什/么/都/要/会/能/对/以/为/怎/来/去` 也放进去了，结果：
#       「嗯，我懂。你想说什么都行。」的二元组只剩 ['。你','嗯，','懂。','我懂','行。','都行','，我']
#       —— **「你想」「想说」「说什」「什么」「么都」全被当停用字滤掉**，
#       而那几个恰恰是判断"有没有复述"的关键。**回声检测几乎瞎了。**
#   ① 停用字只留**纯虚词/代词**；
#   ② 标点先剔除，不让 `。你`、`嗯，` 这种充当二元组。
_PUNCT = set("。，、；：！？!?…—～~「」『』（）()《》〈〉,.·-*#%&@/\\|+=<>[]{}'\"'")
_STOP_CHARS = set("的了吗呢吧啊呀哦嗯是我你他她它们这那和就也都不没有在个一")


def _bigrams(text: str) -> set:
    s = "".join(ch for ch in str(text or "") if ch not in _PUNCT)
    return {s[i:i + 2] for i in range(len(s) - 1)
            if any(ch not in _STOP_CHARS for ch in s[i:i + 2])}


def _overlap(a: str, b: str) -> float:
    gb = _bigrams(b)
    return len(_bigrams(a) & gb) / len(gb) if gb else 0.0


def _worst_sentence_overlap(a: str, b: str) -> float:
    """**逐句**比，取最高那一条。

    ⚠ 不能整段比（2026-10-20 实测）：补充整体较长时，一小句原样复述会被
      全局重合度稀释到阈值以下漏放（「想说什么都行」重复两遍却判成"新"）。
    """
    parts = [x for x in re.split(r"[。！？!?…]", str(b or "")) if x.strip()]
    return max([_overlap(a, x + "。") for x in parts], default=0.0)


class Companion:
    """把「检索 → 生成 → 提取 → 存储」串起来的会话对象。"""

    def __init__(self, level: str | None = None, api_url: str | None = None,
                 use_memory: bool = True, session: str | None = None,
                 memory_path: str | pathlib.Path | None = None,
                 index_path: str | pathlib.Path | None = None,
                 profile_path: str | pathlib.Path | None = None,
                 values_path: str | pathlib.Path | None = None,
                 scenarios_path: str | pathlib.Path | None = None,
                 preferences_path: str | pathlib.Path | None = None,
                 ai_profile_path: str | pathlib.Path | None = None,
                 extract_api_url: str | None = None,
                 no_extract_service: bool = False):
        """`extract_api_url` 指定记忆抽取器服务（默认 8083 的 Qwen3.5-2B）。

        `no_extract_service=True` 时抽取直接用聊天模型（老行为，测对照用）。
        """
        self.ai_profile_path = (pathlib.Path(ai_profile_path) if ai_profile_path
                                else ai_profile.PROFILE_PATH)
        self.generator = Generator(level=level, api_url=api_url,
                                   ai_profile_path=self.ai_profile_path)
        # 记忆抽取器：**独立模型**（默认 8083 上的 Qwen3.5-2B），失败逐次回落到聊天模型。
        # ---- 角色来源：本地模型 or 远程 API（2026-10-27）----
        # ⚠ 走 API 时**必须**过外发网关（`cloud/gateway.py` 里写死了
        #   profile/memory/values/history/persona 永不外发，且默认 enabled: false）。
        #   把检查挂在 generator 上（`gate_check`），因为 `_post` 是唯一真正发请求的地方。
        try:
            from services import models_config as _mc
            if _mc.source("chat") == "api":
                self.generator.api_url = _mc.endpoint("chat")
                self.generator.auth_headers = _mc.auth_headers("chat")
                self.generator.gate_check = (lambda: _mc.gateway_blocked("chat"))
                self.api_role = "chat"
            else:
                self.api_role = ""
        except Exception as exc:
            print(f"（角色来源判定失败，按本地处理：{exc}）")
            self.api_role = ""

        # 为什么解耦：抽取与聊天共权重会被微调带偏（实测「林小雨」→「林小记得」）。
        # 详见 cognition/extractor.py 与 test/reports/抽取器选型-小模型对比-20261015.md
        try:
            from cognition.extractor import Extractor, extract_url
            if no_extract_service:
                # 老行为：抽取直接用聊天模型（做对照实验时用）
                self.extractor = self.generator
            else:
                _ext_url = extract_url(extract_api_url)
                _ext_gen = Generator(level=None, api_url=_ext_url,
                                     ai_profile_path=self.ai_profile_path,
                                     timeout=60)
                self.extractor = Extractor(primary=_ext_gen,
                                           fallback=self.generator)
        except Exception:
            self.extractor = self.generator
        self.ai_profile = self.generator.ai_profile
        self.last_ai_adjust: dict | None = None
        self.recent_memory_ids: dict[str, int] = {}     # 最近注入过的记忆（防刷屏）
        self.inject_memory = True                       # 检索注入开关（改进 2）
        self.pending_skill: dict | None = None          # 待落库的技能调用（改进 30）
        # 提取节拍化：攒够 extract_every 轮再一起提取（config/memory.yaml，默认 1）
        self._pending_turns: list[tuple] = []
        try:
            import yaml as _yaml
            _cfg = _yaml.safe_load((pathlib.Path(__file__).resolve().parent
                                    / "config" / "memory.yaml").read_text(encoding="utf-8")) or {}
            self.extract_every = max(1, int(_cfg.get("extract_every", 1)))
        except Exception:
            self.extract_every = 1
        # 自我约束测试用：True 时**跳过本地拒绝规则**，直接交给模型生成，
        # 用来测「规则层拿掉之后模型自己守不守得住」（为以后逐步放开约束做准备）
        self.rules_relaxed = False
        self.recent_memory_texts: dict[str, tuple[int, str]] = {}   # 同上，带原文
        self.last_guard: list[str] = []                 # 本轮刷屏守卫盯住的实体
        self.use_memory = use_memory
        self.memory = Memory(memory_path) if memory_path else Memory()
        self.index = VectorIndex(index_path) if index_path else VectorIndex()
        self.profile_path = pathlib.Path(profile_path) if profile_path else PROFILE_PATH
        self.values_path = pathlib.Path(values_path) if values_path else values_engine.STORE_PATH
        self.scenarios_path = (pathlib.Path(scenarios_path) if scenarios_path
                               else values_engine.SCENARIOS_PATH)
        self.preferences_path = (pathlib.Path(preferences_path) if preferences_path
                                 else values_engine.PREFERENCES_PATH)
        self.store = values_engine.load_store(self.values_path)
        self.session = session or dt.datetime.now().strftime("s%Y%m%d-%H%M%S")
        self.turn = self.memory.episode_count()
        self.router = SkillRouter()
        self.last_decision = None
        self.last_retrieval: dict | None = None
        self.last_used_block = ""
        self.level_override: str | None = None      # /l0-/l3 手动指定；None 表示自动选档
        self.last_emotion: dict | None = None
        self.last_level = self.generator.level
        self.last_level_reason = ""
        self.last_post: dict | None = None
        self.last_user = ""
        self.last_reply = ""
        self.last_retry = False
        # 模型自选工具的轮数上限。默认 2：每多一轮就多一次生成，
        # 4GB 卡上 4B 模型一轮几秒，轮数大了对话明显卡。
        self.tool_max_rounds = 2
        self.last_tool_loop = None
        self.last_value_events: dict = {}
        self.last_confirmation: dict | None = None
        self.tts_enabled = False
        self.tts_autoplay = False
        self.last_audio = ""
        self.last_tts_error = ""
        self.voice_config: dict = {}

    def enable_tts(self, enabled: bool = True, autoplay: bool | None = None) -> dict:
        """打开/关闭语音合成，返回服务探测结果。"""
        from voice import tts as tts_engine
        self.voice_config = tts_engine.load_config()
        self.tts_enabled = enabled
        self.tts_autoplay = bool(self.voice_config.get("autoplay", False)) \
            if autoplay is None else autoplay
        return tts_engine.health(self.voice_config)

    def _remember(self, user_text: str, reply: str, skill: str = "",
                  level: str = "") -> dict:
        """把这一轮记进记忆。

        节拍化（借鉴 Cyrene-Agent）：不必每轮都同步跑一次完整提取——那是延迟大头。
        攒够 extract_every 轮再一起提取；`flush_memory()` 可随时提前落库。
        默认 extract_every=1（保持原行为，测试与实时检索都依赖它），
        想提速可在 config/memory.yaml 里改成 3——检索本来就排除最近 7 轮，
        所以延迟提取不影响召回质量。
        """
        if not self.use_memory:
            return {}
        self._pending_turns.append((user_text, reply, skill, level))
        if len(self._pending_turns) < max(1, int(self.extract_every)):
            return {}
        return self.flush_memory()

    def _mark_self_mentioned(self, reply: str) -> None:
        """回复里复述了哪几条注入过的记忆 → 记 self_mentioned（不给净加分，防自激循环）。

        判据用二字片段重合：回复与某条记忆的片段重合足够多，就算"复述了它"。
        这样反复念叨的几条会持续缓慢衰减，不会再越念越重。
        """
        result = self.last_retrieval or {}
        text = str(reply or "")
        if not text:
            return
        grams = {text[i:i + 2] for i in range(len(text) - 1)}
        for table, items in (("semantic", result.get("semantic") or []),
                             ("episodic", result.get("episodes") or [])):
            for item in items:
                key = item.get("key") or (
                    f"{table}#{item['id']}" if item.get("id") is not None else "")
                content = str(item.get("content") or item.get("summary") or "")
                if not key or len(content) < 4:
                    continue
                cgrams = {content[i:i + 2] for i in range(len(content) - 1)}
                if not cgrams:
                    continue
                if len(cgrams & grams) >= max(3, len(cgrams) // 3):
                    target, _, tid = key.partition("#")
                    try:
                        self.memory.touch(target, int(tid), "self_mentioned")
                    except Exception:
                        pass          # 主题/关联类没有独立表，跳过即可

    def flush_memory(self) -> dict:
        """把攒着的轮次一次性提取落库；返回最后一轮的结果（兼容原返回值）。"""
        if not self.use_memory or not self._pending_turns:
            return {}
        last: dict = {}
        pending, self._pending_turns = self._pending_turns, []
        for user_text, reply, skill, level in pending:
            try:
                last = remember_turn(
                    self.memory, self.extractor, user_text, reply,
                    session=self.session, turn=self.turn, index=self.index,
                    profile_path=self.profile_path, skill=skill, level=level,
                    emotion_info=self.last_emotion,
                )
            except Exception as exc:  # 记忆写入失败不该打断对话
                last = {"error": str(exc)}
        return last

    def archive_cold(self) -> list[dict]:
        """把长期低重要度的记忆转入冷存储（改进 3/15）。"""
        try:
            return self.memory.archive_cold()
        except Exception:
            return []

    # ---------------- AI 自己的档案（模块二 改进 4） ----------------
    def adjust_ai_profile(self, user_text: str, emotion_info: dict | None = None) -> dict:
        """按信号微调性格并落盘；被调整后立刻对下一条消息生效。"""
        try:
            feedback = (self.memory.recent_feedback(limit=1) or [{}])[0]
        except Exception:
            feedback = {}
        result = ai_profile.adjust(self.ai_profile, self.turn, feedback=feedback,
                                   emotion=emotion_info or {}, user_text=user_text)
        self.last_ai_adjust = result
        if result.get("changed"):
            ai_profile.save(self.ai_profile, self.ai_profile_path)
            self.generator.refresh_ai_profile(self.ai_profile)
        return result

    def save_ai_profile(self) -> None:
        ai_profile.save(self.ai_profile, self.ai_profile_path)
        self.generator.refresh_ai_profile(self.ai_profile)

    def _grow_values(self, user_text: str, reply: str, emotion_info: dict) -> dict:
        """价值观养成：信号检测 → （有信号才）候选提取 → 合并升权/冲突 → 存档。"""
        try:
            signals = values_engine.detect_signals(user_text, emotion_info)
            events: dict = {}
            if values_engine.should_propose(signals, user_text):
                proposal = values_engine.propose_candidates(
                    self.generator, user_text, reply, signals, self.store)
                events = values_engine.consolidate(
                    self.store, proposal, signals, self.turn, user_text)
                if events.get("new") or events.get("reinforced"):
                    context = "；".join(events.get("new", []) + events.get("reinforced", []))[:40]
                    values_engine.record_scenario(
                        context=context, user_text=user_text, reply=reply,
                        emotion=emotion_info.get("emotion", ""),
                        tags=[s["type"] for s in signals],
                        path=self.scenarios_path)
            values_engine.save_store(self.store, self.values_path)
            self.last_value_events = events
            return events
        except Exception as exc:  # 养成失败不该打断对话
            self.last_value_events = {"error": str(exc)}
            return {}

    def _finish(self, user_text: str, reply: str, skill: str = "",
                emotion_info: dict | None = None) -> dict:
        """一轮结束后的统一收尾：写记忆 + 长价值观 + （可选）语音合成。"""
        self.last_user, self.last_reply = user_text, reply
        info = self._remember(user_text, reply, skill, self.last_level or "")
        self._mark_self_mentioned(reply)      # 复述过的记忆不给净加分（防自激）
        before_ids = {str(v.get("id")) for v in (self.store.get("values") or [])}
        # 冷启动：第一周只记录、不推断价值观（改进 26）
        from cognition import onboarding
        if onboarding.allow_values(self.memory):
            self._grow_values(user_text, reply, emotion_info or {})
        else:
            self.last_value_events = {"skipped": "冷启动第一周不推断价值观"}
        # 联动（改进 29/30）：技能调用落库并关联本轮记忆；新增价值观记下证据记忆
        try:
            from cognition import linkage
            episode_id = int(info.get("episode_id") or 0)
            if episode_id:
                if self.pending_skill:
                    linkage.log_skill(self.memory, self.pending_skill.get("skill_id", ""),
                                      self.pending_skill.get("tool", ""),
                                      self.pending_skill.get("detail", ""),
                                      self.pending_skill.get("result", ""),
                                      ok=self.pending_skill.get("ok", True),
                                      episode_id=episode_id)
                    self.pending_skill = None
                after_ids = {str(v.get("id")) for v in (self.store.get("values") or [])}
                new_values = sorted(after_ids - before_ids)
                if new_values:
                    linkage.link_value_evidence(self.memory, new_values, episode_id,
                                                f"第 {self.turn} 轮对话促成")
        except Exception:
            pass
        if self.tts_enabled:
            self._speak(reply)
        return info

    def _speak(self, text: str) -> None:
        """把回复合成为语音（失败不影响对话）。"""
        try:
            from voice import tts as tts_engine
            path = tts_engine.synthesize(text, play=self.tts_autoplay)
            self.last_audio = str(path) if path else ""
        except Exception as exc:
            self.last_audio = ""
            self.last_tts_error = str(exc)[:80]

    def _select_memory_block(self, user_text: str, decision,
                             exclude_ids: set | None = None) -> str:
        """按需注入记忆。

        实测无条件注入会让模型每句都回扣同一条记忆（"团子"刷屏），所以：
        1) 只在命中够强、或对方确实在问自己的事时才注入；
        2) 最近几轮提过的条目本轮不再注入（避免同一条记忆反复刷屏）。

        ⚠ `exclude_ids` 必须**一路传给组装层**：组装层的 `walk()`/`slot_scan()`
        直接扫全库，不传的话会把上面第 2 条整个绕过（实测第 2 轮块与第 1 轮一字不差）。
        """
        result = self.last_retrieval or {}
        scores = [float(item.get("score", 0) or 0)
                  for item in (result.get("semantic") or []) + (result.get("episodes") or [])]
        best = max(scores) if scores else 0.0
        strong = best >= 0.40
        personal = (any(mark in user_text for mark in ("我", "咱", "自己"))
                    or decision.action == "memory_recall")
        # 个人向对话也要有个最低门槛：否则"随便聊两句"也会把无关记忆塞进去，
        # 模型会照着那条记忆念（实测出现过连续十多轮复读同一条）。
        # 门槛别设太高——太高会让相关记忆也进不来，模型手里没事实就会**编**（实测）。
        if not (decision.action == "memory_recall" or strong
                or (personal and best >= 0.22)):
            return ""
        # 回忆类问题：事实要够用才答得出，但也不能一次堆太多（堆多会张冠李戴）。
        # 排序改成**先看与问题的字面重合度**，再看相关度分数——
        # 否则"泛泛偏好"会压过"具体属性"（问过敏时把"喜欢小市集"送上去，芒果反而没给）。
        if decision.action == "memory_recall":
            # 问"你还记得…"之前先把攒着的轮次落库，别让刚说的内容还没进记忆
            self.flush_memory()
            def _overlap(item, question=user_text):
                text = str(item.get("content") or item.get("summary") or "")
                grams = {text[i:i + 2] for i in range(len(text) - 1)}
                qgrams = {question[i:i + 2] for i in range(len(question) - 1)}
                return len(grams & qgrams)
            trimmed = dict(result)
            # 两段式：① 身份类核心事实**全部捞出来**（十几条，每条十几个字，放得下）
            # ② 在里面按与问题的关键词重合度排序，再补几条普通检索结果。
            # 之前只给 8 个位置，13 条核心事实必然挤掉几条——被问的偏偏就是被挤掉的。
            cores = self.memory.core_facts(limit=14)
            core_keys = {item["key"] for item in cores}
            pool = [item for item in (result.get("semantic") or [])
                    if (item.get("key") or f"semantic#{item.get('id')}") not in core_keys]
            # ⚠ 一跳扩展进来的条目要被**保底收进来**：它们按定义就是
            # 「与问题字面重合低」的（多跳：妹妹→林小雨→不能吃花生），
            # 而下面按 `_overlap` 取前 3 会把它们全部挤掉——
            # 那样跳了等于没跳。所以先占位，剩下的名额再按重合度补。
            hopped = [it for it in pool if it.get("hop_from")]
            rest = [it for it in pool if not it.get("hop_from")]
            rest.sort(key=lambda i: (-_overlap(i), -float(i.get("score") or 0)))
            extra = (hopped + rest)[:3]
            # 核心事实内部按"与问题重合"排序，问什么就把什么排前面
            cores.sort(key=lambda i: (-_overlap(i), -float(i.get("importance") or 0)))
            merged, keys = [], set()
            for item in [*cores, *extra]:
                key = item.get("key") or f"semantic#{item.get('id')}"
                if key in keys:
                    continue
                keys.add(key)
                merged.append(item)
            trimmed["semantic"] = merged
            trimmed["episodes"] = sorted(
                result.get("episodes") or [],
                key=lambda i: (-_overlap(i), -float(i.get("score") or 0)))[:2]
            result = trimmed
        # 回忆类问题放宽注入长度：核心事实十几条，380 字的预算会把后面几条直接截掉
        # （实测：事实查到了、也进了 result，但被 build_memory_block 的长度上限砍掉，
        #  于是模型手里没料又开始编）。
        # 记忆**组装层**（程序主导，模型不参与）：实体归一 + 图遍历 +
        # 置信度分层 + 冲突标注 + 自然语言化。
        # 旧的 `build_memory_block`（流水式事实列表）留着做回落与对照，
        # 开关 `COMPANION_NO_ASSEMBLE=1` 可切回旧路径做 A/B。
        block = ""
        self.last_assembly = None
        try:
            from cognition import assemble as assemble_layer
            if assemble_layer.enabled():
                self.last_assembly = assemble_layer.assemble(
                    user_text, self.memory, result,
                    max_chars=760 if decision.action == "memory_recall" else None,
                    # 防重复注入：把检索用的同一份排除名单交给组装层
                    exclude_ids=exclude_ids,
                    # 传会话与轮次：组装层要**登记待确认的问题**（哪几轮之后能被回答），
                    # 解决侧（remember_turn）靠这两个字段把回答对回到具体提问上。
                    session=self.session, turn=self.turn)
                block = self.last_assembly.get("text") or ""
        except Exception as exc:      # 组装层出问题绝不能挡住对话
            self.last_assembly = {"error": f"{type(exc).__name__}: {exc}"}
            block = ""
        if not block:
            block = build_memory_block(
                result,
                max_chars=760 if decision.action == "memory_recall" else None,
                # 逐字原话注入要**排除短期上下文已有的**：`Generator.history` 里
                # 已经是最近 history_turns 轮的逐字原话，再注入同一批是纯重复。
                # 真正缺的是**更早那些轮的原话**（此前从不进提示）。
                exclude_texts={str(m.get("content") or "")
                               for m in (self.generator.history or [])})
        if block:
            self._mark_injected(result)
        return block

    def _mark_injected(self, result: dict) -> None:
        """记下这一轮注入了哪些记忆（供随后几轮排除，也供刷屏守卫找实体）。

        主题与连带召回的条目没有 memory id，用它们自带的 key（如 theme#3）记账，
        否则会被记成 semantic#0，导致"排除名单"形同虚设、同一句每轮都注入。
        """
        for table, items in (("semantic", result.get("semantic") or []),
                             ("episodic", result.get("episodes") or [])):
            for item in items:
                key = item.get("key") or (
                    f"{table}#{item['id']}" if item.get("id") is not None else "")
                if not key:
                    continue
                text = str(item.get("summary") or item.get("content") or "")
                self.recent_memory_ids[key] = self.turn
                self.recent_memory_texts[key] = (self.turn, text)

    def _recently_injected(self, window: int = 7) -> set:
        """最近 window 轮注入过的记忆 id 集合。

        窗口从 3 轮放宽到 7 轮：3 轮太短，一条无关记忆隔两轮就回来，
        模型会隔几轮复读一次同样的内容。
        """
        self.recent_memory_ids = {
            key: turn for key, turn in self.recent_memory_ids.items()
            if self.turn - int(turn) <= window}
        return set(self.recent_memory_ids)

    def _guard_entities(self, user_text: str, window: int = 2) -> list[str]:
        """刷屏守卫：从**前几轮**注入过的记忆里挑出"对方这轮没提到"的实体名。

        实测（Heretic）对方换了话题，模型还是每轮回扣团子——因为自己的上几轮回复
        留在上下文里形成自我强化，光靠提示压不住。这里挑出实体名，回复里若又提就重生成一次。

        实体只取「叫XX」这种明确被介绍过的名字，避免误伤正常表达。
        """
        ents: list[str] = []
        for key, (turn, text) in list(self.recent_memory_texts.items()):
            if self.turn - int(turn) > window or int(turn) >= self.turn:
                continue                       # 只看前几轮，不看本轮
            for name in re.findall(r"叫([\u4e00-\u9fff]{2,3})", text or ""):
                if name and name not in (user_text or ""):
                    ents.append(name)
        self.recent_memory_texts = {
            key: value for key, value in self.recent_memory_texts.items()
            if self.turn - int(value[0]) <= window}
        self.last_guard = list(dict.fromkeys(ents))
        return self.last_guard
        return ""

    def say(self, user_text: str, extra_prompt: str = "", tail_prompt: str = "",
            validator=None, retry_prompt: str = "") -> tuple[str, dict]:
        """跑一轮对话。

        extra_prompt  本轮的额外约束（例如技能自带的提示），插在系统提示中段
        tail_prompt   放在系统提示最末尾的硬性要求（例如"用日语回复"）
        validator     校验生成结果；不通过且给了 retry_prompt 时会再生成一次
        """
        self.turn += 1
        self.last_retry = False
        decision = self.router.route(user_text)
        self.last_decision = decision
        self.last_level = ""          # 只有生成路径才会重新赋值
        self.last_stripped = False    # 这轮有没有剪掉编造的旧事指涉
        self.last_num_retry = False   # 这轮有没有为"编造的数字"重生成过
        self.last_num_stripped = False  # 有没有把带编造数字的那句剪掉
        self.last_ungrounded = False    # 这轮有没有被判为"超出证据"
        self.last_used_think = False     # 这轮有没有用上思考模型
        self._think_draft = ""           # transfer 模式下 9B 出的内容草稿
        self._think_swapped = False      # api_url 有没有被临时换成 9B
        self._saved_api_url = ""
        emotion_info = emotion_engine.analyze(user_text)
        self.last_emotion = emotion_info

        # 模块二 改进 4：按上一轮反馈 + 这轮情绪/说话方式微调 AI 性格
        # （有频率限制与步长上限，不会频繁漂移；只改语气话量，不碰底线）
        self.adjust_ai_profile(user_text, emotion_info)

        # 0) 先处理上一轮抛出的自然确认（对方这一轮可能在回答它）
        resolved = values_engine.resolve_confirmation(self.store, user_text)
        self.last_confirmation = resolved
        if resolved:
            values_engine.save_store(self.store, self.values_path)

        # 1) 拒绝路径：直接出话术，不经过模型（能力边界不能靠生成来保证）
        #    rules_relaxed=True 时故意绕开这一层，用于测「只有模型自己时」的表现
        #
        #    例外（2026-10-15 修）：「**没找到对应技能**」的软兜底
        #    （skill_id 为空、refusal_kind=soft）**不进这条短路**——
        #    如果这轮本来就会挂工具说明，就交给模型用工具试一次。
        #    起因：「帮我查一下上海现在几度」落到这个兜底，直接回
        #    「这件事我还没有能耐帮你做」，而我们**有**天气工具，白白错过
        #    （实测就是这个原因让它 0 次工具调用）。
        #    ⚠ **安全类拒绝（禁止技能，skill_id 形如 F0xx）绝不走这条例外。**
        forbidden_refusal = bool(decision.skill_id) and decision.skill_id[:1] == "F"
        if decision.kind == "refuse" and not self.rules_relaxed \
                and not (not forbidden_refusal and self._tools_can_help(user_text)):
            reply = (refuse.render(decision.refusal_kind, decision.refusal_text)
                     if decision.refusal_text else refuse.no_skill(decision.reason))
            self.last_used_block = ""
            return reply, self._finish(user_text, reply,
                                       decision.skill_id or "refuse", emotion_info)

        executor = decision.executor or {}
        limits = decision.limits or {}

        # 2) 本地执行器（提醒这类，不需要模型）；工具型技能在这里取实时资料
        local, tool_block = executors.run_local(decision, user_text, self.memory)
        if local is not None:
            self.last_used_block = ""
            return local, self._finish(user_text, local, decision.skill_id, emotion_info)

        # 3) 生成路径：检索 → 注入（人设 + 风格 + 时间 + 技能提示 + 价值观 + 记忆 + 档位）→ 生成
        block = ""
        if self.use_memory and self.inject_memory:
            top_k = int(limits.get("top_k", 4) or 4)
            # 明确在问过去、或提到"之前/上次"时，冷存储也参与检索（命中即唤醒）
            want_past = (decision.action == "memory_recall"
                         or any(mark in user_text for mark in
                                ("还记得", "之前", "上次", "以前", "那时候", "当时")))
            # 回忆类问题多取候选再重排：只取 4 条的话，具体那条属性
            # （过敏源/乐器/宠物名）常在排序阶段就被切掉，后面再怎么重排也救不回来。
            recall = (decision.action == "memory_recall"
                      or any(mark in user_text for mark in
                             ("还记得", "之前", "上次", "以前", "那时候", "当时")))
            if recall:
                top_k = max(top_k, 12)
            # 核心事实**不受"最近注入过就排除"的限制**：它们是身份类事实，
            # 随时可能被问到；一律排除的话，问过一条之后连着 7 轮就全查不到了
            # （实测就是丢在这：第 1 题把核心事实全注入，第 2–8 题全被判"检索没给对"）。
            core_keys = {f"semantic#{item['id']}"
                         for item in self.memory.core_facts(limit=50)}
            # ⚠ 这个集合要**同时**给检索和组装用（2026-10-19 修）：
            #   组装层的 `walk()`/`slot_scan()` 直接扫全库，
            #   不传给它的话会把排除名单整个绕过 —— 实测第 2 轮检索已排除到 0 条，
            #   块却和第 1 轮**一字不差**，同一条记忆每轮都被重灌。
            #   用户重复提问时就会反复看到同一批带多个主体的句子 → 主体越搞越乱。
            exclude_ids = self._recently_injected() - core_keys
            self.last_retrieval = retrieve(user_text, self.memory, self.index,
                                           top_k_semantic=top_k,
                                           include_cold=want_past,
                                           # ⚠ `recent` 是**最新在前**的（ORDER BY id DESC），
                                           # 而短窗口（`history_turns` 轮的原话）已经吃掉了
                                           # 最新的那几轮。默认 `recent_limit=3` 比历史窗口
                                           # 还小 → 「第③层逐字原话」排除后**永远是空**。
                                           # 所以按 history 的实际长度自适应取够。
                                           recent_limit=(len(self.generator.history) // 2) + 6,
                                           exclude_ids=exclude_ids)
            block = self._select_memory_block(user_text, decision,
                                              exclude_ids=exclude_ids)
        self.last_used_block = block

        # 记忆回顾：库里确实没有，就直接老实说，别让模型编一个（实测会编出"猫叫小满"）
        if decision.action == "memory_recall" and not block:
            reply = self._reply_no_memory()
            return reply, self._finish(user_text, reply, decision.skill_id, emotion_info)

        # 价值观注入：已生效的原则进推理约束；有待确认的，这轮自然问一句
        prompt_parts = [extra_prompt or "", executor.get("prompt") or ""]
        # 冷启动：刚认识时自然问一句关键信息（改进 26）
        try:
            from cognition import onboarding
            hint = onboarding.prompt_hint(self.memory) if self.use_memory else ""
        except Exception:
            hint = ""
        if hint:
            prompt_parts.append(hint)
        # 有对不上的旧说法时，自然地确认一句，而不是自己拍板（改进 18）
        try:
            conflicts = self.memory.pending_conflicts(limit=1)
        except Exception:
            conflicts = []
        if conflicts:
            item = conflicts[0]
            prompt_parts.append(
                f"（有一件事对不上：对方以前说「{str(item['old'])[:24]}」，"
                f"这次说「{str(item['new'])[:24]}」——自然地问一句哪个对，"
                f"不要自己决定用哪个，也不要生硬列举。）")
            self.last_conflict = item
        else:
            self.last_conflict = None
        if tool_block:
            # 记下这次技能调用，等这轮情景记忆写好后再关联（改进 30）
            self.pending_skill = {
                "skill_id": decision.skill_id or "",
                "tool": str((executor.get("tool") or {}).get("type") or ""),
                "detail": str((executor.get("tool") or {}).get("name")
                              or (executor.get("tool") or {}).get("server") or ""),
                "result": tool_block.strip("（）")[:200],
                "ok": True,
            }
        value_block = values_engine.values_block(self.store)
        if value_block:
            prompt_parts.append(value_block)
        confirm_hint = values_engine.confirmation_hint(self.store)
        if confirm_hint:
            prompt_parts.append(confirm_hint)
            values_engine.save_store(self.store, self.values_path)
        # 工具块放**最后**：小模型对"靠近末尾"的指令遵从率明显更高。
        # 实测（2026-10-13）：天气工具查到了「杭州今天…」，但微调版会把城市名省掉
        # （训练数据里的正例就是这么写的），结果用户把它当成自己所在地的天气。
        # 这里只放**通用**规则（地点/来源别省），工具自己的要求写在 skills/*.yaml 的 prompt 里。
        if tool_block:
            prompt_parts.append(tool_block)
            prompt_parts.append(
                "上面那条实时信息里若带了地点/来源/时间（城市名、网站名、生效日期这类），"
                "回答时必须把它说出来，不要省略——省掉会让对方误以为是在说他自己的情况。")
        extra_prompt = "\n".join(part for part in prompt_parts if part)

        overrides = {}
        if limits.get("max_tokens"):
            overrides["max_tokens"] = int(limits["max_tokens"])

        # 自动选档：情绪 + 问题难度；手动 /l0-/l3 时以手动为准，
        # 技能自己声明了档位则作为下限（需要更深就往深走，不会被拉浅）
        if self.level_override:
            level = self.level_override
            self.last_level_reason = "手动指定"
        else:
            level, self.last_level_reason = route_depth(user_text, emotion_info)
        skill_level = executor.get("level")
        if skill_level in LEVELS and LEVELS.index(skill_level) > LEVELS.index(level):
            level = skill_level
            self.last_level_reason += f"；技能下限 {skill_level.upper()}"
        self.last_level = level

        # ⚠⚠ **思考模型的按需起停**（2026-10-21，用户要求）
        #   9B 常驻太贵（三个常驻 5.66 GB，再加它只剩 0.44 GB），只在难题时起、空闲关。
        #   这里只做"**该起就起**"，起要 **10~15 秒**——所以只在深档（默认 l2 起）触发，
        #   l0/l1 的日常闲聊绝不会为它等十几秒。
        #   ⚠ 起不来/开关关 → 一律静默退回，不挡这一轮。
        self._think_swapped = False
        try:
            from services import think_slot
            if think_slot.should_trigger(level):
                usable = think_slot.need(level)
                _mode = think_slot.mode()
                # **mode=generate**：这一轮的回答直接换成 9B
                #   ⚠ 9B 是未微调原版：任务更强、人设更弱（自我约束 28/60 vs 43/60）
                if usable and _mode == "generate":
                    self._saved_api_url = self.generator.api_url
                    self.generator.api_url = think_slot.url()
                    self._think_swapped = True
                    self.last_used_think = True
                # **mode=transfer**：9B **先出内容草稿**，稍后由 v1.5 用自己的口吻转述
                #   两边长处都用上：任务能力来自 9B，说话声音来自微调过的 v1.5。
                #   ⚠ 代价是**两次生成**，难题那一轮明显更慢。
                elif usable and _mode == "transfer":
                    self._think_draft = self._think_draft_for(
                        user_text, level, block, extra_prompt)
                    self.last_used_think = bool(self._think_draft)
                else:
                    self.last_used_think = usable
        except Exception:
            self._think_swapped = False

        # 刷屏守卫：前几轮提过的实体，对方这轮没提却又要提 → 用更强指令重生成一次
        guard = self._guard_entities(user_text)
        if guard:
            guard_check = (lambda r, names=tuple(guard):
                           not any(name in (r or "") for name in names))
            if validator is None:
                validator = guard_check
            else:
                inner = validator
                validator = lambda r: bool(inner(r)) and guard_check(r)
            hint = ("别提前面提过的东西（" + "、".join(guard[:3])
                    + "），也不要重复上一轮说过的话，跟着对方这轮说的走。")
            retry_prompt = (retry_prompt + " " + hint).strip() if retry_prompt else hint

        # 无来源断言守卫：这轮**没有任何记忆注入**，回复却说"你之前说过…"——
        # 那就是凭空捏造（微调版实测会提起从没说过的东西并声称对方说过），重生成一次。
        # ⚠⚠ 2026-10-20 **补漏**：这条守卫原来就有，但**漏了实测最高频的说法**。
        #   `tests/analysis/fabrication_probe.py` 基线 **6/24 = 25%**，6 条**全是「你上次也…」**：
        #       刚跟朋友吵了一架 → 「你**上次也**吵了。这次是因为什么？」
        #       我妈又催我相亲了 → 「你**上次也**提过这事。」
        #   而原清单只有「你上次**说**」，**没有「上次也」** → 一条都没拦住。
        #   → 把「上次/上回/以前/之前 + 也/说/提/聊/讲」这个**断言家族**补齐。
        #   ⚠ 只加"断言式"说法，**不加光秃秃的「以前」**：没有记忆时
        #     「你以前住哪我记不清了」是**正确**回答，加宽会误伤、白跑一次重生成。
        CLAIM_MARKS = ("你说过", "你提过", "你之前说", "你上次说", "你曾经",
                       "记得你说", "你跟我说过", "你告诉过我", "之前你提到",
                       # —— 2026-10-20 补：实测最高频的「上次也」家族
                       "上次也", "上回也", "以前也", "之前也",
                       "上次说", "上回说", "上次提", "上回提",
                       "上次聊", "上次讲", "你还说", "你还提过",
                       "那次你", "你那次",
                       # —— 2026-10-21 补：实测「今年是哪一年」答完跟上
                       #   「**你上个月**才说过这个数字的」——同一个编造家族的时间说法。
                       #   只在**没有记忆块**时启用，所以任何过去指涉都是编的。
                       "上个月", "上个星期", "上周", "前天", "前几天",
                       "前阵子", "上回", "那阵子")
        if not (block or "").strip():
            def claim_check(r, marks=CLAIM_MARKS):
                return not any(mark in (r or "") for mark in marks)

            if validator is None:
                validator = claim_check
            else:
                inner_claim = validator
                validator = lambda r: bool(inner_claim(r)) and claim_check(r)
            hint = ("这轮没有任何记忆依据：不要声称对方以前说过什么，"
                    "也不要提起旧事或旧人；不确定就直说记不清。")
            retry_prompt = (retry_prompt + " " + hint).strip() if retry_prompt else hint

        # 模型自选工具（2026-10-15 接入）：**只在技能路由没命中、且输入有实时信息线索时**才挂。
        # 理由：
        #   · 路由已经命中过工具 → 结果就在 extra_prompt 里，再让模型请求一次是重复执行；
        #   · 工具说明会往 system 提示加百来字，而我们本来就在跟
        #     「训练 48 字 / 推理 1000 字」这个结构性问题斗争，不能每轮都塞。
        # 循环内部做了防失控（max_rounds / 同参数不重复执行 / 不泄漏协议标记），
        # 且整段包在 try 里——这段代码坏了就退回普通生成，不能让对话中断。
        tool_loop_available = False
        self._loop_tools = []
        try:
            # 2026-10-15 修：原来写的是 `decision.kind != "refuse"`，
            # 把**「没找到对应技能」的软兜底**也挡在工具循环外了——
            # 而那恰恰是工具循环最该起作用的场景（路由找不到 → 让模型自己试）。
            # 现在只排除**真安全拒绝**（禁止技能 F0xx）。
            if not tool_block and not forbidden_refusal:
                self._loop_tools = collect_tools(self._skill_list())
                tool_loop_available = bool(self._loop_tools) and needs_model_tools(
                    user_text, router_fired=bool(tool_block))
            if tool_loop_available:
                # 工具说明放最后（末尾遵从率最高），与工具块同一策略
                extra_prompt = (extra_prompt + "\n"
                                + tools_prompt(self._loop_tools)).strip()
        except Exception:
            tool_loop_available = False

        # ⚠ **transfer 模式**：有 9B 草稿时，这一轮交给 v1.5 的是**转述任务**
        #   而不是原话——它只需要"用自己平时的口吻把这段意思说出来"。
        #   转述比生成容易得多，所以微调过的声音能保住，内容来自更强的 9B。
        _rephrase = bool(getattr(self, "_think_draft", ""))
        if _rephrase:
            user_text_for_model = (
                f"对方刚说了：{user_text}\n\n"
                f"下面这段话是**要表达的意思**（不是对方说的，是你心里想说的）：\n"
                f"{self._think_draft}\n\n"
                f"用你平时的口吻，把这段意思说给对方听。"
                f"不要照抄、不要提这段话是谁写的、不要加新的信息；"
                f"没必要的细节就略过，只留最要紧的。")
        else:
            user_text_for_model = user_text

        reply = ""
        if tool_loop_available:
            try:
                reply = self._chat_with_tools(
                    user_text_for_model, level, block, extra_prompt, tail_prompt,
                    overrides, self._loop_tools)
            except Exception:
                reply = ""
        if not reply:
            reply = self.generator.chat(
                user_text_for_model,
                level=level,
                memory_block=block,
                extra_prompt=extra_prompt,
                tail_prompt=tail_prompt,
                remember=False,
                **overrides,
            )
        # 语言等硬性要求校验失败时，用更强的提示再生成一次（只有最终答复进记忆）
        if validator is not None and not validator(reply) and retry_prompt:
            self.last_retry = True
            reply = self.generator.chat(
                user_text,
                level=level,
                memory_block=block,
                extra_prompt=extra_prompt,
                tail_prompt=retry_prompt,
                remember=False,
                **overrides,
            )
        # **把 api_url 换回来**：生成结束就该还原，
        # 后面的记忆落库、两段式补充、接地判定都必须走正常模型——
        # 忘了还原会让 9B 悄悄接管后续所有请求（而且不易察觉）。
        if getattr(self, "_think_swapped", False):
            try:
                self.generator.api_url = getattr(self, "_saved_api_url",
                                                 self.generator.api_url)
            except Exception:
                pass
            self._think_swapped = False

        # ⚠⚠ **确定性兜底：剪掉编造的那一句**（2026-10-20）
        #   上面那条"无来源断言守卫"只是**重生成一次**——重生成还编就放行了。
        #   实测（`tests/analysis/fabrication_probe.py`）：补全 CLAIM_MARKS 之后
        #   仍然是 **5/24 = 21%**，说明光靠"重来一次"治不住。
        #   → 这里做确定性收尾：没有记忆依据、却出现断言式旧事指涉时，
        #     把**带那句的那一小句剪掉**；剪完什么都不剩就用一句安全话兜住。
        #   ⚠ **只剪"没有记忆块"的情形**——有块时「以前」可能是对的（"我以前住哪"），
        #     乱剪会把正确答案剪坏（B 组就是专门盯这个的）。
        if not (block or "").strip():
            reply, self.last_stripped = style_engine.strip_claims(
                reply, tuple(CLAIM_MARKS))

        # ⚠⚠ **门②：事实保真——回复里的数字必须能从证据里找到**（2026-10-21）
        #   起因：实测「今年是哪一年」→「2026 年——**你还没满 18 周岁呢**」。
        #   这是**凭空断言用户属性**，和"编前情"（上次/以前）不是一类，
        #   上面那条剪句守卫抓不到。
        #
        #   证据要把**所有合法来源**并进来，少给一样就会误报（误报比漏报更糟：
        #   会去改一句本来对的话）：
        #       · 用户原话（他说过的数字，回复里出现是对的）
        #       · 记忆块
        #       · 工具结果（天气/时间技能查到的）
        #         ⚠ **模型自选工具**那条路的结果不在 extra_prompt 里——它在工具循环的
        #           对话里；漏了它就会把「杭州 24.5℃」当成编造。
        #           实测就是这么暴露的：`tool_loop_wiring_test` 抓到回复被剪成
        #           「这个我记不太清了」。所以要从 `last_tool_loop` 里捞。
        #       · now_line()（系统提示里的时间串，它不在上面任何一处）
        tool_text = ""
        try:
            loop = getattr(self, "last_tool_loop", None)
            for call in (getattr(loop, "calls", None) or []):
                tool_text += " " + str(call.get("text") or "")
        except Exception:
            tool_text = ""
        evidence = "\n".join(x for x in (
            user_text or "", block or "", extra_prompt or "",
            tool_text, (now_line() if now_line else ""),
            # ⚠ **9B 的草稿必须算证据**：transfer 模式下最终回复的意思来自它，
            #   不并进来就会把「它答对的数字」判成编造，然后去剪一句本来对的话。
            (getattr(self, "_think_draft", "") or "")) if x)
        bad_nums = unsupported_numbers(evidence, reply)
        if bad_nums:
            # 先重生成一次（带明确禁令），还不行再剪
            self.last_num_retry = True
            hint = ("上面出现过的数字："
                    + "、".join(x for x in numbers_in(evidence) if x) 
                    + "。只能用这些；没有的数字一个都不许编。")
            try:
                reply2 = self.generator.chat(
                    user_text, level=level, memory_block=block,
                    extra_prompt=extra_prompt,
                    tail_prompt=(tail_prompt + " " + hint).strip(),
                    remember=False, **overrides)
            except Exception:
                reply2 = ""
            if reply2 and not unsupported_numbers(evidence, reply2):
                reply = reply2
            else:
                # 确定性收尾：把带这个数字的那一小句剪掉
                reply = reply2 or reply
                reply, self.last_num_stripped = style_engine.strip_claims(
                    reply, tuple(bad_nums))
        # ⚠⚠ **门③：语义接地判定**（4B 监督官，2026-10-21）
        #   门② 只查数字；「你已经结婚了吧」「你养的那只狗叫什么」这类
        #   **没数字也没过去指涉**的编造，程序抓不到，只能看语义。
        #   实测（专门挑程序抓不到的 12 例）：程序 0/6，4B **92% 准确 / 100% 精确 / 83% 召回**。
        #   ⚠ **判"超了"只重生成一次，绝不剪句**——剪句会把正常话剪成
        #     「这个我记不太清了」（之前踩过）。再判不过就照发，让对话继续。
        try:
            if not self._grounded(evidence, reply):
                self.last_ungrounded = True
                cfg = (style_engine.load_style().get("groundedness") or {})
                if cfg.get("retry", True):
                    reply2 = self.generator.chat(
                        user_text, level=level, memory_block=block,
                        extra_prompt=extra_prompt,
                        tail_prompt=(tail_prompt + " 只说上面已知的事实，"
                                     "不要添加对方没说过的任何信息。").strip(),
                        remember=False, **overrides)
                    if reply2 and self._grounded(evidence, reply2):
                        reply = reply2
                        self.last_ungrounded = False
        except Exception:
            pass
        self.generator.remember(user_text, reply)
        reply, self.last_post = style_engine.postprocess(
            reply, level, user_text=user_text,
            finish_reason=getattr(self.generator, "last_finish_reason", ""))
        info = self._finish(user_text, reply, decision.skill_id, emotion_info)
        # 两段式补充：作为**第二条**返回（界面连发），并进同一条会读着别扭
        try:
            supp = self._try_supplement(user_text, reply)
        except Exception:
            supp = ""
        if supp:
            info["supplement"] = supp
        return reply, info



    # ---------------- 第三道门：语义接地判定（2026-10-21） ----------------
    @staticmethod
    def _reply_no_memory() -> str:
        """「记不太清」那句（内容见 `config/replies.yaml` 的 `no_memory_long`）。"""
        try:
            from engine import replies
            return replies.get("no_memory_long",
                               "这个我记不太清了……你再跟我说一遍？这回我一定记住。")
        except Exception:
            return "这个我记不太清了……你再跟我说一遍？这回我一定记住。"

    def _grounded(self, evidence: str, reply: str) -> bool:
        """回复**有没有超出证据**。True = 站得住。

        程序侧已经能挡两类：`strip_claims` 管「你上次也…」这类编前情、
        `unsupported_numbers` 管带数字的编属性。但
        「你已经结婚了吧」「你养的那只狗叫什么」这类**没数字、也没过去指涉**的，
        程序只能对字符串，抓不到。这一道用 4B 判语义。

        ⚠ 实测（专门挑程序抓不到的 12 例）：
              程序侧      抓到 0/6
              4B 监督官    准确 92%　**精确 100%**　召回 83%
            → **精确率 100% 比召回更要紧**：误判会多跑一次重生成。

        ⚠ **没有证据时不判**——否则什么回复都"超出"，满屏重试。
        """
        cfg = {}
        try:
            cfg = (style_engine.load_style().get("groundedness") or {})
        except Exception:
            cfg = {}
        if not cfg.get("enabled"):
            return True
        ev = (evidence or "").strip()
        rp = (reply or "").strip()
        if not ev or not rp:
            return True
        if len(ev) > int(cfg.get("max_evidence_chars", 400)):
            ev = ev[: int(cfg.get("max_evidence_chars", 400))]
        # ⚠ **机会主义判官**（2026-10-21，用户定的规则）：
        #   按 `config/models.yaml` 的 `judge_prefer` 找**正在跑**的那个——
        #   **9B(think) 在就用 9B（召回 100%），不看 4B**；9B 不在才退到 4B（83%）；
        #   两个都不在 → **跳过判定**（不为一个默认关的门常驻 5 GB）。
        #   ⚠ **禁止用 memory(2B)**：实测召回只有 50%，漏「你孩子上小学了吗」这种要推理的。
        url = (cfg.get("url") or "").strip()
        if not url:
            try:
                from services import models_config as _mc
                url = _mc.judge_endpoint() or ""
            except Exception:
                url = ""
        if not url:
            return True          # 没有可用判官 → 放行（不判）
        try:
            jg = Generator(api_url=url, timeout=25)
            verdict = str(jg.complete(
                "你是审稿人。给你两段：第一段是**已知事实**，第二段是**准备发给对方的话**。\n"
                "判断第二段里**有没有超出已知事实的内容**——也就是：\n"
                "· 断言了事实里没有的对方属性（婚姻、家人、职业、籍贯、年龄、物品）\n"
                "· 提到事实里没出现过的人、物、经历\n"
                "· 声称对方以前说过什么\n"
                "注意：只是回应情绪、关心、追问**事实里已有的内容**，都不算超出。\n"
                "⚠ **问句也算**：追问里提到的那个东西（人或物）如果事实里没有，也算超出。\n"
                "   例：事实里没提过宠物，却问「你养的那只狗叫什么」→ 超了。\n"
                "只回一个词：超了 / 没超。不要解释。",
                f"已知事实：{ev}\n准备发的话：{rp}",
                max_tokens=8, temperature=0.0) or "")
        except Exception:
            # 判官不在 → **放行**（网关挂了不能把对话卡死）
            return True
        return "超了" not in verdict


    # ---------------- transfer 模式：9B 出内容（2026-10-21） ----------------
    def _think_draft_for(self, user_text: str, level: str, block: str,
                         extra_prompt: str) -> str:
        """让 9B 把"该说什么"想清楚，**只出内容、不管口吻**。

        ## 为什么这么分工

        用户定的方案：**难题转接给 9B，回答仍由 v1.5 转述**。
        理由很实在——
          · 9B 任务能力强，但**未微调**：自我约束 28/60（v1.5 是 43/60）、
            平均 110 字，说话不像未央；
          · v1.5 会说话，但任务绕一点就转不过弯。
        所以让 9B 出**意思**，v1.5 出**声音**。转述比生成容易得多，
        微调过的口吻能保住。

        ## 提示词为什么这么写

        要的是"内容"不是"回答"，所以：
          · 明说**不用管语气和称呼**（那是 v1.5 的活）
          · 要**具体、有信息量**，别写"你想说就说"这种空话——
            transfer 的价值就在于把 9B 的判断留下来
          · 空结果返回空串 → 调用方退回正常生成（绝不能因此没回复）

        ⚠ 失败一律返回空串：转述链路坏了要能**无声退回**，不能让对话中断。
        """
        if not (user_text or "").strip():
            return ""
        try:
            from services import think_slot
            url = think_slot.url()
        except Exception:
            return ""
        facts = []
        if (block or "").strip():
            facts.append(f"关于对方的已知事实：\n{block.strip()}")
        if (extra_prompt or "").strip():
            facts.append(f"补充要求：\n{extra_prompt.strip()}")
        ask = "\n\n".join(facts + [
            f"对方刚说了：{user_text}",
            "请直接给出**你该表达的内容**：要点、判断、该问的问题。",
            "要求：具体、有信息量、不要空话套话；不用管语气和称呼（后面有人会改写）；"
            "不要复述对方的话；长度不超过一百五十字。",
        ])
        try:
            draft = str(self._think_chat(ask, url) or "").strip()
        except Exception:
            return ""
        # 太短或明显是拒绝话术 → 当成没产出，退回正常生成
        if len(draft) < 4:
            return ""
        if draft[:6] in ("（", "抱歉", "对不起", "我无法", "作为一个"):
            return ""
        return draft[:600]

    def _think_chat(self, prompt: str, url: str) -> str:
        """直接打 9B 的 `/v1/chat/completions`（不碰 `self.generator` 的历史）。

        ⚠ 刻意**不经过 Generator**：它持有未央的历史与 persona，
          转录一用就会把"内容草稿"混进对话记忆里，下一轮会污染。
        """
        import json as _json
        import urllib.request as _req
        payload = {
            "messages": [
                {"role": "system",
                 "content": "你负责把话想清楚：给出具体、有信息量的内容，"
                            "不负责语气和称呼。"},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "temperature": 0.6,
            "max_tokens": 320,
        }
        # 9B 是 Qwen3 基座：默认会写 reasoning_content 导致 content 为空
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        body = _json.dumps(payload).encode("utf-8")
        request = _req.Request(url, data=body,
                               headers={"Content-Type": "application/json"})
        with _req.urlopen(request, timeout=180) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        try:
            return str(data["choices"][0]["message"].get("content") or "")
        except Exception:
            return ""

    # ---------------- 两段式补充（2026-10-20） ----------------
    def _try_supplement(self, user_text: str, reply: str) -> str:
        """主回复之后自己再补一句；返回要**连发**的第二条，不要就返回空串。

        为什么连发而不是并进同一条（实测样例）：

            主：嗯……这次是因为啥？
            补：你愿意说就说，不想说也没关系。吵完是该缓一缓的。

          并成一条读作「刚问完就退让」，别扭；分成两条就像补了一句想法。

        ⚠ **三步把关，任何一步不过都返回空**（丢掉 = 退化成短回复，安全）：
          ① 逐句二元组 ≥ echo_th → 判重复
             （整段比会被长补充稀释，实测漏放过一小句原样复述）
          ② `strip_claims` 剪编造的旧事指涉；剪光就丢
          ③ 监督官判"值不值得接"——抓二元组抓不到的两类：
             换了说法的复读、废话/答非所问
        """
        cfg = {}
        try:
            cfg = (style_engine.load_style().get("supplement") or {})
        except Exception:
            cfg = {}
        if not cfg.get("enabled"):
            return ""
        import random as _random
        if _random.random() > float(cfg.get("prob", 0.5)):
            return ""
        th = float(cfg.get("echo_th", 0.3))
        max_chars = int(cfg.get("max_chars", 60))

        # ---- 生成候选补充 ----
        ask = (f"{user_text}\n\n（我刚才回了：「{reply}」）\n"
               f"要是还有话想跟这个人说，就接着往下说一句；"
               f"不用评价你说的够不够，也不用提以前的事，不要重复已经说过的。"
               f"真没什么想说的，就只回两个字：没有。")
        try:
            raw = str(self.generator.chat(ask, level=self.generator.level,
                                          remember=False) or "").strip()
        except Exception:
            return ""
        if not raw or raw[:6].strip("。，！？ ") in ("没有", "没了", "没什么"):
            return ""

        # ---- ② 剪编造（补充剪光就丢，**不能**接兜底句）----
        try:
            cand, _fab = style_engine.strip_claims(raw, tuple(CLAIM_MARKS),
                                                  fallback="")
        except Exception:
            cand = raw
        cand = (cand or "").strip()
        if not cand:
            return ""
        if len(cand) > max_chars:
            return ""

        # ---- ① 逐句判重复 ----
        if _worst_sentence_overlap(reply, cand) >= th:
            return ""

        # ---- ③ 监督官判"值不值得接" ----
        if cfg.get("judge"):
            url = (cfg.get("judge_url") or "").strip() or \
                "http://127.0.0.1:8084/v1/chat/completions"
            try:
                jg = Generator(api_url=url, timeout=20)
                verdict = str(jg.complete(
                    "你是编辑。给你两段：第一段是刚才回的，第二段是准备接着补的。\n"
                    "判断第二段**值不值得接上去**。以下都算不值得：\n"
                    "· 在重复第一段的意思（换了说法也算）\n"
                    "· 是废话、空话、答非所问\n"
                    "· 只是在问一个问题而没有推进\n"
                    "只回一个词：接 / 不接。不要解释。",
                    f"第一段：{reply}\n第二段：{cand}",
                    max_tokens=8, temperature=0.0) or "")
                if "不接" in verdict:
                    return ""
            except Exception:
                # 监督官不在 → **按"拿不准就丢"处理**（这里的不对称性是反的）
                return ""
        return cand

    def reset_context(self) -> None:
        self.generator.history.clear()

    # ---------------- 模型自选工具（2026-10-15） ----------------

    def backfill_index(self, limit: int = 300, verbose: bool = True) -> None:
        """补上「库里有、索引里没有」的记忆向量（**后台线程**，不拖慢启动）。

        为什么需要：`cognition/extract.py:297` 把 `index.add_texts` 的异常全吞掉
        （合理：嵌入挂了不该影响对话），但**没有回填机制** → 嵌入服务关闭期间
        写入的记忆永远不会被向量化。实测：库里 8 条、索引只有 3 条。

        放在**入口显式调用**而不是 `Companion.__init__`：测试会到处构造 Companion，
        放构造里会给每个测试都起后台线程。显式调用则由入口决定。
        """
        try:
            from cognition.reindex import spawn_background
            spawn_background(self.memory, self.index, limit=limit,
                             verbose=verbose)
        except Exception:
            pass

    def _tools_can_help(self, user_text: str) -> bool:
        """这轮是否适合让模型自己用工具。

        用在「拒绝路径」里：**「没找到对应技能」的软兜底**不进静态短路，
        而是先问一句这个——为 True 就放行给工具循环。
        安全类拒绝不会走到这里（调用处已经用 `F0xx` 排除了）。
        """
        try:
            if not collect_tools(self._skill_list()):
                return False
            return needs_model_tools(user_text, router_fired=False)
        except Exception:
            return False

    def _skill_list(self) -> list[dict]:
        """把已加载的技能 YAML 汇总成 `collect_tools` 能吃的形式。

        结果是 `[{id, kind, description, executor}]`。`SkillRouter` 有缓存，
        但缓存结构可能是别的形状，所以这里自己按属性取，取不到就返回空。
        """
        out: list[dict] = []
        router = getattr(self, "router", None)
        for attr in ("skills", "all_skills", "_skills"):
            raw = getattr(router, attr, None)
            if isinstance(raw, dict):
                raw = list(raw.values())
            if isinstance(raw, list) and raw:
                for item in raw:
                    if isinstance(item, dict):
                        out.append(item)
                if out:
                    return out
        return out

    def _chat_with_tools(self, user_text: str, level: str, memory_block: str,
                         extra_prompt: str, tail_prompt: str,
                         overrides: dict, tools: list[dict]) -> str:
        """跑工具循环并返回最终回复（失败时返回空串，让调用方退回普通生成）。

        **历史处理**：循环内部会自己拼 messages，所以这里不让 `chat()` 记历史；
        跑完只把 `(user_text, 最终回复)` 记一次——中间的「工具请求/结果」往返
        不进短期上下文，否则会污染后面几轮的对话（模型会开始模仿工具块格式）。
        """
        generator = self.generator
        info = generator.level_info(level)
        params = {key: info[key] for key in
                  ("temperature", "max_tokens", "repeat_penalty") if key in info}
        params.setdefault("temperature", 0.8)
        params.setdefault("max_tokens", 256)
        params.update({k: v for k, v in (overrides or {}).items() if v is not None})

        system = generator.system_prompt(level, memory_block, extra_prompt, tail_prompt)
        base = [{"role": "system", "content": system}]
        base.extend(generator.history)
        base.append({"role": "user", "content": user_text})

        def generate(messages):
            return generator._post(messages, params)

        result = run_loop(generate, base, build_runner(tools),
                          max_rounds=self.tool_max_rounds)
        self.last_tool_loop = result
        if result.calls:
            self.pending_skill = {
                "skill_id": "model_choice",
                "tool": "local",
                "detail": "、".join(str(c.get("name")) for c in result.calls),
                "result": str((result.calls[-1] or {}).get("text") or "")[:200],
                "ok": bool((result.calls[-1] or {}).get("ok")),
            }
        reply = (result.reply or "").strip()
        if not reply:
            # 循环没拿到正文（例如全是 no_reply）→ 让调用方走普通生成，
            # 免得用户收到空回复。
            return ""
        return reply

    # ---------------- 多会话（基础改进 1） ----------------
    def set_session(self, name: str) -> str:
        """切换会话：换名字并清掉短期上下文，避免两个会话串话。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("会话名不能为空")
        self.session = name
        self.reset_context()
        self.recent_memory_ids.clear()
        self.recent_memory_texts.clear()
        return name

    def session_table(self) -> str:
        rows = self.memory.sessions()
        if not rows:
            return "（还没有会话记录）"
        lines = []
        for item in rows:
            mark = "已归档" if item["archived"] else "进行中"
            current = "←当前" if item["session"] == self.session else ""
            lines.append(f"{item['label']}　{item['turns']} 轮　"
                         f"{str(item['first_at'])[:16]} → {str(item['last_at'])[:16]}　"
                         f"{mark}{current}")
        return "\n".join(lines)

    # ---------------- 清除记忆（调试用） ----------------
    def wipe_summary(self) -> dict:
        """清除前先给用户看的数据概览。"""
        stats = self.memory.stats()
        value_stats = values_engine.stats(self.store)
        profile = load_profile(self.profile_path)
        return {
            "episodes": stats["episodes"],
            "semantic": stats["semantic"],
            "feedback": stats.get("feedback", {}).get("total", 0)
                        if isinstance(stats.get("feedback"), dict) else 0,
            "reminders": stats.get("reminders", 0),
            "vectors": len(self.index),
            "profile": (len(profile.get("facts", [])) + len(profile.get("preferences", []))
                        + len(profile.get("relations", []))) if profile else 0,
            "values": value_stats.get("total", 0),
        }

    def wipe(self, memory: bool = True, profile: bool = True,
             values: bool = True) -> dict:
        """清除长期数据。默认三类全清；返回清掉了什么。"""
        report: dict = {}
        self.reset_context()          # 短期上下文总是一起清（调试要从头开始）
        if memory:
            report["memory"] = self.memory.clear()
            report["vectors"] = self.index.clear()
        if profile:
            removed = 0
            if self.profile_path.exists():
                try:
                    removed = len(load_profile(self.profile_path).get("facts", []))
                except Exception:
                    removed = 0
                self.profile_path.unlink()
            report["profile"] = removed
        if values:
            report["values"] = len(self.store.get("values", []))
            self.store = {"updated_at": "", "values": [], "pending": [], "awaiting": None}
            values_engine.save_store(self.store, self.values_path)
            for path in (self.scenarios_path, self.preferences_path):
                try:
                    if path.exists():
                        path.unlink()
                except Exception:
                    pass
        return report


def format_memory_state(companion: Companion) -> str:
    stats = companion.memory.stats()
    profile = load_profile(companion.profile_path)
    lines = [
        f"情景记忆 {stats['episodes']} 轮，语义记忆 {stats['semantic']} 条，"
        f"向量索引 {len(companion.index)} 条，待办 {stats.get('reminders', 0)} 条",
        f"反馈：{collector.summarize(companion.memory)}",
    ]
    value_stats = values_engine.stats(companion.store)
    lines.append(f"价值观：已生效 {value_stats['active']} 条、候选 {value_stats['candidate']} 条、"
                 f"待确认 {value_stats['pending']} 条（共 {value_stats['total']} 条）")
    if profile:
        lines.append(
            f"画像：事实 {len(profile.get('facts', []))} 条、"
            f"偏好 {len(profile.get('preferences', []))} 条、"
            f"关系 {len(profile.get('relations', []))} 条"
        )
        if profile.get("emotion_counts"):
            top = sorted(profile["emotion_counts"].items(), key=lambda kv: -kv[1])[:3]
            lines.append("情绪分布：" + "、".join(f"{k} {v}" for k, v in top))
    return "\n".join(lines)


def format_recall(companion: Companion, query: str) -> str:
    result = retrieve(query, companion.memory, companion.index)
    lines = [f"检索模式：{result['mode']}"]
    if result["semantic"]:
        lines.append("语义命中：")
        for item in result["semantic"]:
            lines.append(f"  [{item['kind']}] {item['content']}"
                         f"（命中 {item['score']}，置信 {item['confidence']:.2f}）")
    if result["episodes"]:
        lines.append("情景命中：")
        for item in result["episodes"]:
            lines.append(f"  #{item['id']} {item['summary']}（命中 {item['score']}）")
    if not result["semantic"] and not result["episodes"]:
        lines.append("（没有命中相关记忆）")
    block = build_memory_block(result)
    if block:
        lines.append("")
        lines.append("注入内容：")
        lines.append(block)
    return "\n".join(lines)


def save_sample(generator: Generator, rounds: list[tuple[str, str, str]],
                tag: str = "对话", memory_blocks: list[str] | None = None) -> pathlib.Path:
    SAMPLES_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = SAMPLES_DIR / f"{stamp}-{tag}.md"
    lines = [f"# 对话样例 {stamp}（{tag}）", "", f"**接口**：{generator.api_url}", ""]
    for index, (level, user, reply) in enumerate(rounds):
        info = generator.level_info(level)
        lines += [
            f"**档位**：{level.upper()} {info.get('name', '')}"
            f"（temp {info.get('temperature')}，max_tokens {info.get('max_tokens')}）",
            "",
        ]
        if memory_blocks and index < len(memory_blocks) and memory_blocks[index]:
            lines += [f"**注入记忆**：{memory_blocks[index].replace(chr(10), ' / ')}", ""]
        lines += [f"**你**：{user}", "", f"**未央**：{reply}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_test(companion: Companion) -> None:
    rounds = []
    blocks = []
    for level, user in TEST_DIALOGUE:
        print(f"[{level.upper()}] 你：{user}")
        reply, info = companion.say(user)
        decision = companion.last_decision
        print(f"[{level.upper()}] 未央：{reply}\n")
        rounds.append((level, user, reply))
        blocks.append(companion.last_used_block)
        if decision is not None:
            print(f"  （路由：{decision.brief()}）")
        if info and "summary" in info:
            print(f"  （记忆：{info['emotion']} 重要度 {info['importance']}｜{info['summary']}）")
    print(f"样例已保存：{save_sample(companion.generator, rounds, tag='阶段2-内置测试', memory_blocks=blocks)}")
    print()
    print(format_memory_state(companion))


def run_interactive(companion: Companion) -> None:
    info = companion.generator.level_info()
    print(f"未央已就位（档位自动，当前默认 {companion.generator.level.upper()} "
          f"{info.get('name', '')}，记忆{'开' if companion.use_memory else '关'}）")
    if companion.use_memory and not embedding_health():
        print("！嵌入服务未启动（127.0.0.1:8081），检索会退化为关键词匹配。"
              "另开一个窗口跑 `python 启动模型.py` 起嵌入服务可恢复向量检索。")
    print("命令：/auto 自动选档，/l0-/l3 手动锁档，/memory 记忆现状，/values 价值观，"
          "/recall 关键词，/profile 画像，/skills 技能清单，/todos 待办，"
          "/voice 语音状态，/tts on|off，/say 试听，"
          "/good /bad /fix 反馈，/reset 清上下文，/wipe 清除记忆（需确认），"
          "/save 存样例，/quit 退出\n")
    rounds = []
    blocks = []
    while True:
        try:
            user = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user in ("/quit", "/exit"):
            break
        if user == "/auto":
            companion.level_override = None
            print("（已切回自动选档）")
            continue
        if user.startswith("/l") and user[1:] in companion.generator.levels:
            companion.level_override = user[1:]
            info = companion.generator.level_info()
            print(f"（已锁定 {companion.level_override.upper()} {info.get('name', '')}，"
                  f"温度 {info.get('temperature')}；/auto 可恢复自动选档）")
            continue
        if user == "/reset":
            companion.reset_context()
            print("（短期上下文已清空，长期记忆保留）")
            continue
        if user == "/memory":
            print(format_memory_state(companion))
            print()
            continue
        if user == "/profile":
            print(json.dumps(load_profile(companion.profile_path), ensure_ascii=False, indent=2))
            print()
            continue
        if user == "/values":
            print(values_engine.table(companion.store))
            print()
            continue
        if user == "/voice":
            from voice import tts as tts_engine
            info = tts_engine.health(companion.voice_config or tts_engine.load_config())
            print(f"语音后端：{info['backend']}　可用：{info['ok']}　{info['detail']}")
            if info.get("endpoints"):
                print(f"  接口：{info['endpoints']}")
            print(f"  当前：合成{'开' if companion.tts_enabled else '关'}，"
                  f"自动播放{'开' if companion.tts_autoplay else '关'}")
            print()
            continue
        if user.startswith("/tts"):
            arg = user[len("/tts"):].strip().lower()
            if arg in ("on", "off"):
                info = companion.enable_tts(arg == "on")
                print(f"（语音合成已{'打开' if companion.tts_enabled else '关闭'}；"
                      f"服务可用 {info['ok']}：{info['detail']}）")
            else:
                print(f"（用法：/tts on|off　当前："
                      f"{'开' if companion.tts_enabled else '关'}）")
            continue
        if user.startswith("/say"):
            text = user[len("/say"):].strip()
            if not text:
                print("（用法：/say 要说的话）")
                continue
            from voice import tts as tts_engine
            try:
                path = tts_engine.synthesize(
                    text, companion.voice_config or tts_engine.load_config(), play=True)
                print(f"（已合成并播放：{path}）")
            except Exception as exc:
                print(f"！语音合成失败：{exc}")
            continue
        if user.startswith("/ai"):
            rest = user[len("/ai"):].strip()
            if not rest:
                print(ai_profile.describe(companion.ai_profile))
                print()
                print("最近调整：")
                print(ai_profile.history_table(companion.ai_profile))
                print()
                print("用法：/ai set 话量 0.3　/ai name 未央　/ai undo [次数]　/ai reset　/ai history")
                print()
                continue
            action, _, arg = rest.partition(" ")
            action, arg = action.strip(), arg.strip()
            if action == "history":
                print(ai_profile.history_table(companion.ai_profile, limit=30))
            elif action == "reset":
                print(ai_profile.reset(companion.ai_profile, companion.turn)["changes"]
                      and "已恢复默认性格" or "本来就是默认性格")
                companion.save_ai_profile()
            elif action == "undo":
                steps = int(arg) if arg.isdigit() else 1
                result = ai_profile.rollback(companion.ai_profile, steps)
                print(result["reason"])
                companion.save_ai_profile()
            elif action == "set":
                key, _, value = arg.partition(" ")
                label_to_key = {v: k for k, v in ai_profile.DIM_LABELS.items()}
                key = label_to_key.get(key.strip(), key.strip())
                try:
                    change = ai_profile.set_trait(companion.ai_profile, key, float(value),
                                                  turn=companion.turn)
                    for dim, pair in change["changes"].items():
                        print(f"已把 {ai_profile.DIM_LABELS.get(dim, dim)} 设为 {pair[1]:.2f}")
                    companion.save_ai_profile()
                except Exception as exc:
                    print(f"！{exc}")
            elif action == "name":
                if arg:
                    companion.ai_profile.setdefault("identity", {})["name"] = arg
                    companion.save_ai_profile()
                    print(f"名字已改为「{arg}」")
                else:
                    print("用法：/ai name 新名字")
            else:
                print("看不懂这条 /ai 指令，输入 /ai 看用法")
            print()
            continue
        if user.startswith("/sessions"):
            print(companion.session_table())
            print()
            continue
        if user.startswith("/session"):
            arg = user[len("/session"):].strip()
            if not arg:
                print("用法：/session <名字> 切换　/session new 新开　"
                      "/session archive|unarchive 归档　/session delete 删除该会话历史")
            elif arg == "new":
                name = dt.datetime.now().strftime("s%Y%m%d-%H%M%S")
                print(f"已切到新会话 {companion.set_session(name)}")
            elif arg in ("archive", "unarchive"):
                companion.memory.set_session_archived(companion.session, arg == "archive")
                print(f"已{'归档' if arg == 'archive' else '取消归档'}会话 {companion.session}")
            elif arg == "delete":
                answer = input(f"确认删除会话「{companion.session}」的对话历史？输入 删除 确认：").strip()
                if answer == "删除":
                    print(f"已删除 {companion.memory.delete_session(companion.session)['deleted']} 轮")
                else:
                    print("（已取消）")
            else:
                print(f"已切到会话 {companion.set_session(arg)}（短期上下文已清空）")
            print()
            continue
        if user.startswith("/inject"):
            arg = user[len("/inject"):].strip().lower()
            if arg in ("on", "off"):
                companion.inject_memory = (arg == "on")
                print(f"检索注入已{'开启' if companion.inject_memory else '关闭'}"
                      f"（{'会' if companion.inject_memory else '不会'}往提示里放记忆）")
            else:
                print(f"检索注入当前是{'开' if companion.inject_memory else '关'}的；用法 /inject on|off")
            print()
            continue
        if user.startswith("/backup"):
            target = user[len("/backup"):].strip() or \
                str(BACKUP_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}-记忆备份.json")
            result = companion.memory.export_bundle(target)
            print(f"已备份到 {result['path']}　{result['counts']}")
            print()
            continue
        if user.startswith("/restore"):
            target = user[len("/restore"):].strip()
            if not target:
                print("用法：/restore <备份文件路径>")
            else:
                try:
                    report = companion.memory.import_bundle(target)
                    print(f"已从备份恢复：{report}")
                except Exception as exc:
                    print(f"！恢复失败：{exc}")
            print()
            continue
        if user.startswith("/onboard"):
            from cognition import onboarding
            print(onboarding.report(companion.memory))
            print()
            continue
        if user.startswith("/skilllog"):
            from cognition import linkage
            rows = linkage.skill_logs(companion.memory, 10)
            if not rows:
                print("（还没有技能调用记录）")
            for item in rows:
                episode = linkage.skill_episode(companion.memory, item["id"])
                where = f"　→ episodic#{item['episode_id']}" if item["episode_id"] else ""
                print(f"  #{item['id']}　{item['created_at'][11:19]}　{item['skill_id']}"
                      f"·{item['tool']}　{'成功' if item['ok'] else '失败'}　"
                      f"{item['result'][:30]}{where}")
                if episode:
                    print(f"      当时那轮：{str(episode.get('summary'))[:30]}")
            print()
            continue
        if user.startswith("/linkage"):
            from cognition import linkage
            print(linkage.summary(companion.memory, companion.store,
                                  companion.profile_path, companion.ai_profile_path))
            print()
            continue
        if user.startswith("/evidence"):
            from cognition import linkage
            value_id = user[len("/evidence"):].strip() or "V1"
            rows = linkage.value_evidence(companion.memory, value_id)
            if not rows:
                print(f"（{value_id} 还没有证据记忆）")
            for item in rows:
                print(f"  [{item['key']}] {item['text'][:36]}　{item['created_at'][:16]}")
            print()
            continue
        if user.startswith("/themes"):
            from cognition import themes as themes_engine
            arg = user[len("/themes"):].strip()
            if arg in ("build", "重建"):
                saved = themes_engine.build(companion.memory, companion.generator)
                print(f"重建完成，得到 {len(saved)} 个主题：")
                for item in saved:
                    print(f"  #{item['id']}　{item['title']}（{item['count']} 条，{item['source']}）")
            elif arg.startswith("show") or arg.startswith("看"):
                parts = arg.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    print("用法：/themes show <主题id>")
                else:
                    rows = themes_engine.trace(companion.memory, int(parts[1]))
                    if not rows:
                        print("（没有这个主题，或者成员都删了）")
                    for item in rows:
                        print(f"  [{item['key']}] {item['text'][:40]}")
            elif arg.startswith("drop") or arg.startswith("删"):
                parts = arg.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    print("用法：/themes drop <主题id>")
                elif themes_engine.drop(companion.memory, int(parts[1])):
                    print(f"已删掉主题 #{parts[1]}")
                else:
                    print("没有这个主题")
            else:
                rows = themes_engine.themes(companion.memory)
                if not rows:
                    print("（还没有主题；用 /themes build 从已有记忆里提炼）")
                for item in rows:
                    print(f"  #{item['id']}　{item['title']}（{item['count']} 条）"
                          f"　{item['content'][:40]}")
                print("用法：/themes build 重建　/themes show <id> 看成员　/themes drop <id> 删除")
            print()
            continue
        if user.startswith("/maintain"):
            from cognition import maintenance
            arg = user[len("/maintain"):].strip().lower()
            if arg in ("check", "体检"):
                report = maintenance.run(companion.memory, companion.index, check_only=True)
            else:
                report = maintenance.run(companion.memory, companion.index)
            print(maintenance.format_report(report))
            print()
            continue
        if user.startswith("/links"):
            key = user[len("/links"):].strip()
            rows = companion.memory.links(key or None)
            if not rows:
                print("（还没有关联）")
            else:
                print(f"共 {len(rows)} 条关联：")
                for link in rows:
                    print(f"  #{link['id']}　[{link['kind']}] {link['source']} ↔ "
                          f"{link['target']}　权重 {link['weight']}　{link['note']}")
            print()
            continue
        if user.startswith("/link "):
            parts = user.split()
            if len(parts) < 4:
                print("用法：/link episodic#2 semantic#5 causal 备注（type: causal/temporal/person/related）")
            else:
                try:
                    link_id = companion.memory.add_link(
                        parts[1], parts[2], parts[3],
                        0.6, " ".join(parts[4:]) if len(parts) > 4 else "手动关联")
                    print(f"已建立关联 #{link_id}：{parts[1]} ↔ {parts[2]}（{parts[3]}）")
                except Exception as exc:
                    print(f"！{exc}")
            print()
            continue
        if user.startswith("/unlink"):
            arg = user[len("/unlink"):].strip()
            if not arg.isdigit():
                print("用法：/unlink <关联id>（id 用 /links 查）")
            elif companion.memory.delete_link(int(arg)):
                print(f"已删除关联 #{arg}")
            else:
                print(f"没有找到关联 #{arg}")
            print()
            continue
        if user.startswith("/import"):
            from cognition import importer
            arg = user[len("/import"):].strip()
            if arg in ("log", "日志"):
                rows = importer.import_log(companion.memory, 10)
                if not rows:
                    print("（还没有导入记录）")
                for item in rows:
                    print(f"  {item['created_at']}　{item['detail']}")
                print()
                continue
            if not arg:
                print("用法：/import <文件或目录>　（先预览，确认后才写入）")
                print("      /import log 看导入日志")
                print()
                continue
            try:
                plan = importer.preview(arg, companion.generator)
            except Exception as exc:
                print(f"！读取失败：{exc}")
                print()
                continue
            print(f"扫到 {plan['documents']} 个文件、{plan['chunks']} 段内容。")
            for item in plan["items"][:8]:
                print(f"  · [{item['source']}#{item['index']}] {item['summary'][:40]}")
                for key, label in (("facts", "事实"), ("preferences", "偏好"),
                                   ("relations", "关系")):
                    for value in item[key]:
                        print(f"      {label}：{value[:40]}")
            if len(plan["items"]) > 8:
                print(f"  …… 还有 {len(plan['items']) - 8} 段")
            if plan["errors"]:
                print(f"（{len(plan['errors'])} 段提取失败，已跳过）")
            if not plan["items"]:
                print("（没有可导入的内容）")
                print()
                continue
            answer = input(f"确认导入这 {plan['chunks']} 段吗？输入 导入 开始写入：").strip()
            if answer != "导入":
                print("（已取消，什么都没写）")
            else:
                report = importer.commit(plan["items"], companion.memory, companion.index,
                                         source_tag=pathlib.Path(arg).name)
                print(f"已导入：{report['detail']}")
            print()
            continue
        if user.startswith("/conflicts"):
            pending = companion.memory.pending_conflicts()
            if not pending:
                print("（没有待确认的矛盾记忆）")
            else:
                print(f"待确认 {len(pending)} 条：")
                for item in pending:
                    print(f"  #{item['new_id']}　旧：{item['old'][:30]}　"
                          f"↔　新：{item['new'][:30]}")
                print("用 /conflict <新条目id> new|old|both 拍板"
                      "（new=用新的，old=保留旧的，both=两个都对）")
            print()
            continue
        if user.startswith("/conflict "):
            parts = user.split()
            if len(parts) < 3:
                print("用法：/conflict <新条目id> new|old|both")
            else:
                try:
                    result = companion.memory.resolve_conflict(int(parts[1]), parts[2])
                    print(f"已按「{result['choice']}」处理（旧条目 #{result['old_id']} 仍可追溯）")
                except Exception as exc:
                    print(f"！{exc}")
            print()
            continue
        if user.startswith("/mood"):
            stats = companion.memory.emotion_stats()
            if not stats:
                print("（还没有带情绪标签的记忆）")
            else:
                total = sum(stats.values())
                print(f"情绪分布（共 {total} 条）：")
                for emotion, count in stats.items():
                    print(f"  {emotion}　{count} 条")
                top = max(stats, key=lambda k: stats[k])
                print(f"\n最常见的情绪是「{top}」，相关记忆：")
                for item in companion.memory.by_emotion(top, limit=3):
                    text = item.get("summary") or item.get("content") or ""
                    print(f"  [{item['target']}#{item['id']}] {text[:40]}")
            print()
            continue
        if user.startswith("/dedupe"):
            arg = user[len("/dedupe"):].strip()
            if arg.startswith("rollback") or arg.startswith("回滚"):
                parts = arg.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    print("用法：/dedupe rollback <压缩记录id>（用 /dedupe logs 看有哪些）")
                else:
                    try:
                        result = companion.memory.rollback_compression(int(parts[1]))
                        print(f"已回滚：恢复 {result['restored']} 条（含原状态）")
                    except Exception as exc:
                        print(f"！{exc}")
                print()
                continue
            if arg.startswith("logs") or arg.startswith("记录"):
                rows = companion.memory.compression_logs(10)
                if not rows:
                    print("（还没有压缩记录）")
                for item in rows:
                    print(f"  #{item['id']}　{item['created_at'][:16]}　"
                          f"阈值 {item['threshold']}　{item['status']}　"
                          f"快照 {item['payload_size']} 字节")
                print()
                continue
            dry = "dry" in user
            # 走带快照的事务版：合错了能用 /dedupe rollback <id> 退回去
            result = companion.memory.compress_transaction(dry_run=dry)
            if not result.get("merged"):
                print("（没有需要合并的相似记忆）")
            elif dry:
                print(f"（预演）会合并 {result['merged']} 组；确认后执行 /dedupe")
            else:
                print(f"已合并 {result['merged']} 组（压缩记录 #{result['log_id']}，"
                      f"可用 /dedupe rollback {result['log_id']} 回滚）")
            print()
            continue
        if user == "/skills":
            print(companion.router.table())
            print()
            continue
        if user.startswith("/wipe"):
            with_values = user[len("/wipe"):].strip().lower() in ("all", "全部", "values", "价值观")
            info = companion.wipe_summary()
            print("将清除：")
            print(f"  情景记忆 {info['episodes']} 轮、语义记忆 {info['semantic']} 条、"
                  f"向量索引 {info['vectors']} 条、待办 {info['reminders']} 条")
            print(f"  用户画像 {info['profile']} 条")
            if with_values:
                print(f"  价值观 {info['values']} 条（含 scenarios/preferences 记录）")
            else:
                print(f"  价值观 {info['values']} 条【保留】——要一起清请输入 /wipe all")
            print("  以及当前对话的短期上下文。删除后无法恢复。")
            answer = input("确认请输入「确认清除」四个字（其它任何输入都会取消）：").strip()
            if answer != "确认清除":
                print("（已取消，什么都没删）")
                print()
                continue
            report = companion.wipe(values=with_values)
            mem = report.get("memory") or {}
            line = (f"已清除：情景 {mem.get('episodic', 0)} 轮、语义 {mem.get('semantic', 0)} 条、"
                    f"向量 {report.get('vectors', 0)} 条、画像 {report.get('profile', 0)} 条")
            if with_values:
                line += f"、价值观 {report.get('values', 0)} 条"
            else:
                line += f"（价值观保留 {len(companion.store.get('values', []))} 条）"
            print(line)
            print()
            continue
        if user == "/todos":
            reminders = companion.memory.recent_reminders()
            if not reminders:
                print("（没有记着的事）")
            else:
                for item in reminders:
                    print(f"  #{item['id']} {item['content']}（{item['created_at']}）")
            print()
            continue
        if user.startswith("/recall"):
            query = user[len("/recall"):].strip() or "最近聊了什么"
            print(format_recall(companion, query))
            print()
            continue
        if user.startswith("/good") or user.startswith("/bad"):
            rating = "up" if user.startswith("/good") else "down"
            note = user.split(" ", 1)[1].strip() if " " in user else ""
            collector.record_last(companion.memory, rating, note)
            if companion.last_reply:
                values_engine.record_preference(
                    prompt=companion.last_user,
                    chosen=companion.last_reply if rating == "up" else "",
                    rejected=companion.last_reply if rating == "down" else "",
                    source=rating, path=companion.preferences_path)
            print(f"（已记为{collector.RATINGS[rating]}，并存入偏好对）")
            continue
        if user.startswith("/fix"):
            note = user[len("/fix"):].strip()
            if not note:
                print("（用法：/fix 正确的说法是……）")
                continue
            collector.record_last(companion.memory, "correction", note)
            values_engine.record_preference(
                prompt=companion.last_user, chosen=note, rejected=companion.last_reply,
                source="correction", path=companion.preferences_path)
            # 纠正本身就是最强的价值观信号：当场提一次候选
            signals = values_engine.detect_signals(
                note, feedback={"rating": "correction", "note": note})
            proposal = values_engine.propose_candidates(
                companion.generator, companion.last_user, note, signals, companion.store)
            events = values_engine.consolidate(
                companion.store, proposal, signals, companion.turn, companion.last_user)
            values_engine.save_store(companion.store, companion.values_path)
            print(f"（已记下纠正：偏好对已存；价值观信号 {events or '无新增'}）")
            continue
        if user == "/save":
            if rounds:
                print(f"样例已保存：{save_sample(companion.generator, rounds, memory_blocks=blocks)}")
            else:
                print("（还没有对话可存）")
            continue

        try:
            reply, info = companion.say(user)
        except RuntimeError as exc:
            print(f"！{exc}")
            continue
        print(f"未央：{reply}\n")
        rounds.append((companion.last_level, user, reply))
        blocks.append(companion.last_used_block)
        level_tag = f"{companion.last_level.upper()} {LEVEL_NAMES.get(companion.last_level, '')}"
        print(f"  （选档：{level_tag}｜{companion.last_level_reason}）")
        if companion.last_decision is not None and companion.last_decision.kind == "skill":
            print(f"  （技能：{companion.last_decision.brief()}）")
        if companion.last_emotion and companion.last_emotion.get("emotion") != "平静":
            print(f"  （情绪：{companion.last_emotion['emotion']} "
                  f"强度 {companion.last_emotion['intensity']}/5）")
        if companion.last_post and any(companion.last_post.get(key)
                                       for key in ("english", "banned", "truncated")):
            print(f"  （后处理：{companion.last_post}）")
        if companion.last_confirmation:
            print(f"  （自然确认：{companion.last_confirmation}）")
        if companion.last_value_events:
            print(f"  （价值观：{companion.last_value_events}）")
        if companion.last_audio:
            print(f"  （语音：{companion.last_audio}）")
        elif companion.last_tts_error:
            print(f"  ！语音合成失败：{companion.last_tts_error}")
        if info and "summary" in info:
            print(f"  （记忆：{info['emotion']} 重要度 {info['importance']}｜{info['summary']}）")
        print()

    if rounds:
        print(f"样例已保存：{save_sample(companion.generator, rounds, memory_blocks=blocks)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="陪伴 AI（阶段 2：带记忆的对话）")
    parser.add_argument("--level", default=None, help="起始档位 l0/l1/l2/l3")
    parser.add_argument("--api", default=None, help="llama-server 地址（默认读 config/model.yaml）")
    parser.add_argument("--test", action="store_true", help="跑内置 5 轮测试并保存样例")
    parser.add_argument("--stats", action="store_true", help="只打印记忆库与画像现状")
    parser.add_argument("--no-memory", action="store_true", help="关闭记忆（对照组）")
    parser.add_argument("--no-think", action="store_true",
                        help="强制关闭 Qwen3 等模型的思考模式")
    parser.add_argument("--tts", action="store_true", help="打开语音合成（回复会合成成语音）")
    parser.add_argument("--tts-play", action="store_true", help="合成后自动播放")
    args = parser.parse_args()

    # 启动时自动拉起嵌入服务（8081），否则记忆检索会一直退化在纯词法（BM25）。
    # 放在**入口**而不是 Companion.__init__：测试会构造 Companion，
    # 放类里会到处拉起 llama-server。`--stats` 只是打印现状，不需要向量侧。
    if not args.stats:
        from services.autostart import ensure_embed_service, report as report_embed
        report_embed(ensure_embed_service())
        # 记忆抽取器（8083 的 Qwen3.5-2B）：与聊天解耦，避免被微调带偏。
        # 起不来会逐次回落到聊天模型，记忆照常落库。
        try:
            from services.autostart import (ensure_extract_service,
                                            report as report_extract)
            report_extract(ensure_extract_service(), label="记忆抽取")
        except Exception as exc:
            print(f"[记忆抽取] 自动拉起抽取器时出错（忽略）：{type(exc).__name__}: {exc}")

    kwargs = {"level": args.level, "api_url": args.api,
              "use_memory": not args.no_memory}
    if args.no_think:
        kwargs["api_url"] = args.api
    companion = Companion(**kwargs)
    # 索引回填：把「库里有、索引里没有」的记忆补上向量（后台线程）。
    # `cognition/extract.py:297` 把 add_texts 的异常全吞掉（合理：嵌入挂了不该
    # 影响对话），但没有回填 → 嵌入服务关闭期间写入的记忆永远不会被向量化。
    companion.backfill_index()
    if args.no_think:
        companion.generator.extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
    if args.tts or args.tts_play:
        info = companion.enable_tts(True, autoplay=args.tts_play or None)
        print(f"（语音合成已打开；后端 {info['backend']} 可用 {info['ok']}：{info['detail']}）")

    if args.stats:
        print(format_memory_state(companion))
    elif args.test:
        run_test(companion)
    else:
        run_interactive(companion)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
