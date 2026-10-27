# -*- coding: utf-8 -*-
"""工具调用循环：模型请求工具 → 执行 → 结果回填 → 再生成，直到不再请求。

## 为什么要有循环，而不是「查一次就完」

现有链路是「路由命中 → 执行一次 → 结果拼进提示 → 生成」
（`skills/executors.py: run_local`），**只有一轮**。这带来三个做不到的事：

1. **模型不能自己决定要查什么**。问法一变，关键词路由就漏。
2. **查到的不够也不能再查**。比如先查天气发现是雨天，想再查「雨天适合干什么」——
   现有链路没有第二轮。
3. **多工具联动做不到**（A 的结果当 B 的输入）。

## 设计要点

- **生成函数是注入的**。`run_loop(..., generate=...)` 里 `generate(messages) -> str`
  由调用方提供，所以这一层**完全不依赖 engine/generate.py**，可以离线测试
  （见 `tests/tool_loop_test.py`，用桩函数覆盖所有分支）。
- **权限仍由 `tools/executor.py` 把关**。解析通过 ≠ 允许执行；
  被拒的工具把拒绝原因回填给模型，让它改用别的方式或直接说明。
- **三道防失控**：
  1. `max_rounds`：最多几轮；
  2. **同（工具+参数）不重复执行**，直接把上次结果回填——防模型原地打转；
  3. `max_calls_per_round`：一轮最多认几个（解析层已限，这里再兜一层）。
- **失败不中断对话**：工具失败也回填「失败 + 原因」，让模型能解释或换路，
  而不是整轮报错（照 VCPToolBox `FileFetcherServer` 的
  「预取失败降级返回原值，别把工具链打断」）。
"""
from __future__ import annotations

import json

from tools.protocol import (END, START, ToolRequest, format_result, parse,
                            strip_reasoning)


class ToolLoopResult:
    """循环结果。"""

    __slots__ = ("reply", "rounds", "calls", "stopped")

    def __init__(self, reply: str, rounds: int, calls: list[dict],
                 stopped: str = "done") -> None:
        self.reply = reply            # 最终回复（已剥掉工具块）
        self.rounds = rounds          # 实际跑了几轮生成
        self.calls = calls            # 每次调用的记录 [{name, args, ok, text}]
        self.stopped = stopped        # done / max_rounds / empty

    def __repr__(self) -> str:
        return (f"ToolLoopResult(rounds={self.rounds}, calls={len(self.calls)}, "
                f"stopped={self.stopped!r})")


def _key(req: ToolRequest) -> str:
    """一次调用的身份：工具名 + 参数。用来判「原地打转」。"""
    return req.name + "|" + json.dumps(req.args, ensure_ascii=False, sort_keys=True)


# ---- 决定「这一轮要不要让模型自己用工具」 -------------------------------------

# 实时信息线索：出现这些词时，才把工具说明挂上。
#
# ⚠ 这里**故意和技能触发词不完全重合**，而且必须包含技能触发词**之外**的说法。
# 踩过的坑：最初我把线索写成「天气/下雨/气温/冷不冷/带伞」——那几乎就是
# 天气技能 `skills/extension/weather.yaml` 的 `triggers.keywords` 原样抄了一遍。
# 结果是：凡是命中线索的句子**路由也一定命中**，于是 `not tool_block` 恒为假，
# 整套「模型自选工具」**永远不会触发（死代码）**。
# 所以这里的线索要针对**路由漏掉的问法**：
#   「外面冷吗」「要穿外套吗」「现在几度」——技能触发词里没有这些，路由会漏，
#   而这正是需要模型自己查一下的情况。
# 误报的代价很低：代价只是 system 提示多一段工具说明，模型可以选择不用。
REALTIME_MARKS = (
    # 时间/此刻（技能触发词里没有，但明显在问当下）
    # 注意：**不能单放「今天」**——「我今天有点累」会被误判成要查天气，
    # 实测就是这条误报。所以只保留必须搭配的写法，以及「现在」这类强信号。
    "现在", "此刻", "这会儿", "外面",
    "今天外面", "明天外面", "明天", "后天",
    # 体感/穿衣（最容易漏的缝）
    "冷吗", "热吗", "冷不冷", "热不热", "穿什么", "穿外套", "加衣", "带伞",
    "要不要穿", "需不需要穿",
    # 温度/降水
    "多少度", "气温", "温度", "下雨", "下雪", "刮风", "天气",
    # 明确的查实时信息意图
    "查一下", "帮我查", "实时", "最新",
)


def needs_model_tools(user_text: str, router_fired: bool = False) -> bool:
    """这一轮要不要让模型自己请求工具。

    - `router_fired=True`（技能路由已命中并执行过工具）→ **不要**。
      结果已经在提示里了，再让模型请求一次就是重复执行同一个工具。
    - 否则看有没有实时信息线索。**这些线索要覆盖路由漏掉的问法**，
      否则门槛会和技能触发词重合、永远不触发（见上面 REALTIME_MARKS 的说明）。
    """
    if router_fired:
        return False
    text = str(user_text or "")
    return any(mark in text for mark in REALTIME_MARKS)


def collect_tools(skills: list[dict] | None = None,
                  allow: tuple[str, ...] = ("weather",)) -> list[dict]:
    """收集**可以交给模型自选**的工具。

    默认只放白名单里的只读工具（目前只有 `weather`，`permission: network`）。
    为什么不把所有工具都给模型：`command`/`mcp` 类有副作用，让模型自由触发风险大；
    而且工具说明越长，对 system 提示的污染越重。

    返回项里带 `_def`（技能里那份完整工具定义）——执行时要拿它去过权限，
    只靠工具名没法做权限检查。
    """
    out: list[dict] = []
    for skill in (skills or []):
        executor = (skill or {}).get("executor") or {}
        if executor.get("type") != "tool":
            continue
        tool = executor.get("tool") or {}
        name = str(tool.get("name") or "")
        if name not in allow:
            continue
        args: list[str] = []
        if name == "weather":
            args = ["city"]
        out.append({"name": name, "desc": str((skill or {}).get("description") or ""),
                    "args": args, "_def": dict(tool),
                    "_skill_id": str((skill or {}).get("id") or ""),
                    "_skill_kind": str((skill or {}).get("kind") or "")})
    # 去重（同一个工具可能被多个技能声明）
    seen: set[str] = set()
    uniq: list[dict] = []
    for item in out:
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        uniq.append(item)
    return uniq


def build_runner(tools: list[dict]):
    """把收集到的工具表包成一个 `run_tool(name, args) -> dict`，供 `run_loop` 用。

    执行仍然走 `tools/executor.py: run_tool`，所以**权限检查、超时、重试、
    降级、日志全都不变**——这一层只是把「工具名 + 模型给的参数」转成调用。
    模型给的参数是字符串，这里只做最小必要转换（city 这类本来是文本参数）。
    """
    by_name = {str(t.get("name")): t for t in (tools or [])}

    def runner(name: str, args: dict) -> dict:
        item = by_name.get(str(name))
        if item is None:
            return {"ok": False, "text": "",
                    "detail": f"没有这个工具：{name}（可用的只有 "
                              f"{'、'.join(by_name) or '（空）'}）"}
        from tools.executor import run_tool

        tool_def = dict(item.get("_def") or {})
        # 参数并进工具定义：weather 这类工具从 text 里取城市，
        # 模型明确给了 city 就优先用它（比再从整句话里猜更准）。
        text = " ".join(str(v) for v in (args or {}).values()).strip()
        if args:
            tool_def = {**tool_def, "args": dict(args)}
        return run_tool(tool_def, text=text,
                        skill_id=str(item.get("_skill_id") or ""),
                        skill_kind=str(item.get("_skill_kind") or ""))

    return runner


def run_loop(generate, messages: list[dict], run_tool, *,
             max_rounds: int = 3, max_calls_per_round: int = 4,
             on_call=None) -> ToolLoopResult:
    """跑工具循环。

    - `generate(messages) -> str`：调模型。由调用方包住 engine/generate.py。
    - `run_tool(name, args) -> dict`：`{"ok": bool, "text": str, "detail": str}`。
      调用方包住 `tools/executor.py:run_tool`（权限检查在里面）。
    - `on_call(record)`：可选回调，用来记日志；**只读记录，不要改**。
    - `messages` 会被复制，**不修改调用方传进来的列表**。

    返回 `ToolLoopResult`；`reply` 是**剥掉了工具块**的正文。
    """
    convo = [dict(m) for m in (messages or [])]
    calls: list[dict] = []
    seen: dict[str, dict] = {}
    reply = ""
    rounds = 0

    for round_no in range(1, max(1, max_rounds) + 1):
        rounds = round_no
        reply = generate(convo) or ""
        requests = parse(reply, max_calls=max_calls_per_round)

        if not requests:
            # 没请求工具 → 这就是最终回复
            return ToolLoopResult(strip_reasoning(reply).strip(), rounds, calls, "done")

        # 执行本轮请求，把结果作为新的 user 消息回填
        feedback: list[str] = []
        silent = True
        for req in requests:
            key = _key(req)
            if key in seen:
                record = dict(seen[key], repeated=True)
                feedback.append(format_result(
                    req.name, record["ok"], record["text"],
                    echo="（这次和上次是同一个请求，直接用了上次结果）"))
                # 这里也要更新 silent：否则重复请求会被当成 no_reply，
                # 走进「静默收尾」提前返回，正文就丢了（实测踩过）。
                if not req.no_reply:
                    silent = False
                continue
            try:
                result = run_tool(req.name, req.args) or {}
            except Exception as exc:                    # 工具炸了也不能中断对话
                result = {"ok": False, "text": "", "detail": f"{type(exc).__name__}: {exc}"}
            ok = bool(result.get("ok"))
            text = str(result.get("text") or result.get("detail") or "").strip()
            record = {"name": req.name, "args": req.args, "ok": ok, "text": text}
            seen[key] = record
            calls.append(record)
            if on_call:
                try:
                    on_call(record)
                except Exception:
                    pass
            feedback.append(format_result(req.name, ok, text, echo=req.echo))
            if not req.no_reply:
                silent = False

        # 全部 no_reply → 模型不想说话，直接收尾（不浪费一轮生成）
        if silent and requests:
            return ToolLoopResult("", rounds, calls, "done")

        # 把模型这轮的原文（含工具块）记进去，再把结果作为新的 user 轮
        convo.append({"role": "assistant", "content": reply})
        convo.append({"role": "user",
                      "content": "（工具执行结果）\n" + "\n".join(feedback)})

    # 轮数用完：再生成一次拿正文。
    # **这次不再解析工具块**——模型可能还在吐工具，但那已经不执行了。
    # 但也不能把原始工具块当正文返回（用户会看到 <<<[TOOL_REQUEST]>>> 这种乱码），
    # 所以给它两次机会并明确告知"没得调了"；实在不行就把工具块剥掉再返回。
    for _attempt in range(2):
        final = generate(convo) or ""
        if not parse(final, max_calls=1):
            return ToolLoopResult(strip_reasoning(final).strip(), rounds, calls,
                                  "max_rounds")
        convo.append({"role": "assistant", "content": final})
        convo.append({"role": "user",
                      "content": "（工具调用次数已用完，请直接用平常的语气回答，"
                                 "不要再用工具格式）"})
    # 两次都还在吐工具块：剥掉标记后返回，至少不留协议乱码
    cleaned = _drop_tool_blocks(strip_reasoning(final))
    return ToolLoopResult(cleaned.strip(), rounds, calls, "max_rounds")


def _drop_tool_blocks(text: str) -> str:
    """把工具块整段删掉（用于兜底：模型不听话时不让协议标记泄漏给用户）。"""
    out = text
    while True:
        start = out.find(START)
        if start < 0:
            return out
        end = out.find(END, start)
        if end < 0:
            return out[:start]        # 未闭合：从标记处截断
        out = out[:start] + out[end + len(END):]
