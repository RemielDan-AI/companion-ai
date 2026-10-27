# -*- coding: utf-8 -*-
"""工具调用协议：让**模型自己请求工具**，而不是只由技能路由预先选定。

## 为什么需要它

现在工具是「技能路由命中 → 直接执行 → 结果拼进提示让模型照实转述」
（`skills/executors.py: run_local`）。模型**没有选择权**，也不会二次决策。
把天气技能命中后，工具就必然执行；没命中就必然不执行。

问题在于：
1. 问法千变万化，路由是关键词匹配，必然漏（实测「我住在哪」这类问法很容易脱靶）；
2. 模型拿到工具结果后如果发现不够，**没法再查一次**；
3. 多个工具要联动时（先查 A 再拿 A 的结果查 B），现有链路做不到。

## 为什么用中文定界符而不是 function-calling

我们跑的是 4B 量化模型，**function-calling 支持很差**。而文本协议：
- 「始」「末」几乎不会被正常内容撞上，也不会与 markdown/JSON 冲突；
- 出错时模型更容易通过 few-shot 学会（样板直接放在提示里）。

这套设计的来源是 VCPToolBox（`modules/vcpLoop/toolCallParser.js`），
但**只取了需要的部分**，明确**不要**它的：
- 模糊匹配（把 `<<<` 放宽到 2–4 个尖括号、`「始」` 放宽成 4 种写法）——
  那是给能力极弱的模型兜底的，代价是**误伤**（内容里偶然出现的尖括号会被当成工具块）；
- `maid`/`river`/`vref`/`ink`/`jev` 那一堆元字段——是给分布式与记忆场景用的，
  单机上只会变成提示词噪声。

## 协议格式

    <<<[TOOL_REQUEST]>>>
    tool_name: 「始」weather「末」
    city: 「始」杭州「末」
    <<<[END_TOOL_REQUEST]>>>

多行值用 ESCAPE 区包住：

    <<<[TOOL_REQUEST]>>>
    tool_name: 「始」write_note「末」
    text: 「始ESCAPE」第一行
    第二行「末ESCAPE」
    <<<[END_TOOL_REQUEST]>>>

## 安全取向（重要）

- **思考区未闭合 → 后面内容全部丢弃**。理由是「宁可少执行，不可误执行」：
  模型没写完思考就崩了，后半段的工具块很可能是残缺或错乱的，
  执行它的风险大于收益。（沿用 VCPToolBox 的判断，`toolCallParser.js:60-64`）
- 解析器**只管解析，不执行**。执行与循环在 `run_loop`，权限仍由
  `tools/executor.py` 的 `check_permission` 把关——**解析通过不等于允许执行**。
"""
from __future__ import annotations

import re

START = "<<<[TOOL_REQUEST]>>>"
END = "<<<[END_TOOL_REQUEST]>>>"
FIELD_START = "「始」"
FIELD_END = "「末」"
ESC_START = "「始ESCAPE」"
ESC_END = "「末ESCAPE」"

# 元字段：解析时单拿出来，不当作工具参数
META_KEYS = {"tool_name", "no_reply", "mark_history", "echo"}

# 思考区标签（qwen3 的 think 由 chat_template 处理，这里兜住模型自己写出来的）
_THINK_RE = re.compile(r"<\s*(/?)\s*(think(?:ing)?)\b[^>]*>", re.IGNORECASE)
_KEY_RE = re.compile(r"[A-Za-z_][\w\-]*\s*:")


class ToolRequest:
    """一次工具请求。"""

    __slots__ = ("name", "args", "no_reply", "mark_history", "echo", "raw")

    def __init__(self, name: str, args: dict, no_reply: bool = False,
                 mark_history: bool = False, echo: str = "", raw: str = "") -> None:
        self.name = name
        self.args = args
        self.no_reply = no_reply          # 只调工具、不必再回话（静默执行）
        self.mark_history = mark_history  # 这次调用要写进历史
        self.echo = echo                  # 附在结果后面的一句给模型看的话
        self.raw = raw

    def __repr__(self) -> str:
        return (f"ToolRequest(name={self.name!r}, args={self.args!r}, "
                f"no_reply={self.no_reply}, mark_history={self.mark_history})")


def strip_reasoning(text: str) -> str:
    """去掉思考区。

    **未闭合时，起始标签之后的内容全部丢弃**——理由见模块开头「安全取向」。
    用深度计数而不是简单正则，是为了正确处理嵌套与多个思考区。
    """
    if not text:
        return ""
    chunks: list[str] = []
    cursor = 0
    depth = 0
    for match in _THINK_RE.finditer(text):
        if depth == 0:
            chunks.append(text[cursor:match.start()])
        if match.group(1) == "/":
            if depth > 0:
                depth -= 1
        else:
            depth += 1
        cursor = match.end()
    if depth == 0:
        chunks.append(text[cursor:])
    return "".join(chunks)


def _find_block_end(text: str, start: int) -> int:
    """找工具块的结束标记，**跳过 ESCAPE 区**（转义区里的「末」不算结束）。"""
    cursor = start
    while cursor < len(text):
        esc = text.find(ESC_START, cursor)
        end = text.find(END, cursor)
        if end < 0:
            return -1
        if esc < 0 or end < esc:
            return end
        # 结束标记落在转义区之后 → 跳过整个转义区继续找
        esc_close = text.find(ESC_END, esc + len(ESC_START))
        if esc_close < 0:
            return -1
        cursor = esc_close + len(ESC_END)
    return -1


def _scan_fields(body: str) -> list[tuple[str, str]]:
    """扫出 `key: value` 字段。值可以是 `「始」…「末」`、ESCAPE 多行区、或到行尾。

    为什么允许「到行尾」这种宽松写法：4B 模型经常漏掉结束标记。
    漏了就当这一行是值——**比整块丢掉更有用**，而且值本来就短。

    注意**必须对整块扫描、不能按行扫**：ESCAPE 区里可能包含换行，
    按行扫会把多行值截成第一行（这就是最初的 bug）。
    """
    fields: list[tuple[str, str]] = []
    cursor = 0
    length = len(body)
    while cursor < length:
        match = _KEY_RE.search(body, cursor)
        if not match:
            break
        key = match.group(0)[:-1].strip()
        pos = match.end()
        while pos < length and body[pos] in " \t":
            pos += 1

        if body.startswith(ESC_START, pos):
            # ESCAPE 区：整段吞掉，直到 ESC_END
            pos += len(ESC_START)
            close = body.find(ESC_END, pos)
            if close < 0:
                value, cursor = body[pos:], length
            else:
                value, cursor = body[pos:close], close + len(ESC_END)
            fields.append((key, value))
            continue

        if body.startswith(FIELD_START, pos):
            pos += len(FIELD_START)
            close = body.find(FIELD_END, pos)
            if close < 0:
                # 漏了结束标记：取到本行末尾
                eol = body.find("\n", pos)
                end = length if eol < 0 else eol
                fields.append((key, body[pos:end].strip()))
                cursor = end
            else:
                fields.append((key, body[pos:close]))
                cursor = close + len(FIELD_END)
            continue

        # 没有定界符：取到行尾
        eol = body.find("\n", pos)
        end = length if eol < 0 else eol
        fields.append((key, body[pos:end].strip()))
        cursor = end
    return fields


def parse(text: str, max_calls: int = 4) -> list[ToolRequest]:
    """从模型回复里解析出所有工具请求。解析不出就返回空列表（不抛错）。

    `max_calls` 限一次最多认几个——模型偶尔会一口气吐十几个块，
    全执行既慢又危险。
    """
    if not text:
        return []
    body_text = strip_reasoning(text)
    out: list[ToolRequest] = []
    cursor = 0
    while len(out) < max_calls:
        start = body_text.find(START, cursor)
        if start < 0:
            break
        body_start = start + len(START)
        end = _find_block_end(body_text, body_start)
        if end < 0:
            # 块没闭合：丢弃（同「未闭合思考区」的取向）
            break
        block = body_text[body_start:end]
        cursor = end + len(END)

        name = ""
        args: dict[str, str] = {}
        no_reply = mark_history = False
        echo = ""
        for key, value in _scan_fields(block):
            low = key.lower()
            if low == "tool_name":
                name = value.strip()
            elif low == "no_reply":
                no_reply = value.strip().lower() in ("true", "1", "yes", "是")
            elif low == "mark_history":
                mark_history = value.strip().lower() in ("true", "1", "yes", "是")
            elif low == "echo":
                echo = value.strip()
            elif low in META_KEYS:
                continue
            else:
                args[key] = value.strip()
        if name:
            out.append(ToolRequest(name, args, no_reply, mark_history, echo,
                                   raw=block.strip()))
    return out


def format_result(name: str, ok: bool, text: str, echo: str = "") -> str:
    """把工具结果拼成回填给模型的一段。**明确标注来源**，避免模型把它当自己说过的话。"""
    status = "成功" if ok else "失败"
    line = f"（工具 {name} 调用{status}：{text or '（空）'}）"
    if echo:
        line += f" {echo}"
    return line


def tools_prompt(tools: list[dict], include_example: bool = True) -> str:
    """把可用工具拼成给模型看的说明 + 样板。

    `tools` 形如 `[{"name": "weather", "desc": "查天气", "args": ["city"]}]`。

    ## 写法是 A/B 测出来的（2026-10-15，v1.2）

        老写法（模板 + 列表 + 样板）  **0/16**
        纯样板                     1/16
        **样板 + 强指令放最后       4/16**   ← 现在这版
        极简                       **0/16**

    老写法为什么是 0：它的第一段是**占位符模板**
    （`tool_name: 「始」下面例子里的工具名「末」`），模型会照抄那个占位符。
    ⚠ 这个缺陷当年被 `tests/tool_protocol_test.py` 的「自洽性」一项警告过
    （样板被解析成工具名"下面例子里的工具名"），当时只补了第二段真样板、
    **没删掉占位模板**——教训：自洽性测试报警要处理根因，不是绕过。
    所以现在**只给一个可直接照抄的真样板**，不给模板。

    强指令放**最后**（末尾遵从率最高，与工具块同一策略）。

    这段文字同时用来生成微调正例，所以**训练与推理必须逐字一致**：
    改这里等于改训练数据里的提示词，两边要一起动。
    """
    if not tools:
        return ""
    first = tools[0]
    name = first.get("name")
    first_arg = (first.get("args") or ["city"])[0]

    lines: list[str] = []
    if len(tools) == 1:
        lines.append(f"查实时信息的工具叫 {name}，参数是 {first_arg}。")
    else:
        lines.append("可用工具：")
        for tool in tools:
            args = "、".join(tool.get("args") or []) or "无参数"
            lines.append(f"  - {tool.get('name')}：{tool.get('desc', '')}（参数：{args}）")
        lines.append(f"下面以 {name} 为例。")

    if include_example:
        lines.append("")
        lines.append("用户问：外面冷吗")
        lines.append("你这样回：")
        lines.append(START)
        lines.append(f"tool_name: {FIELD_START}{name}{FIELD_END}")
        lines.append(f"{first_arg}: {FIELD_START}杭州{FIELD_END}")
        lines.append(END)
        lines.append("")
        lines.append("注意：当用户问现在的天气、温度、要不要穿外套、外面冷不冷，"
                     "而你**没有**这些实时信息时，**必须**先输出上面那个块，"
                     "不要自己编一个温度，也不要说“你自己看天气预报”。"
                     f"用户没说城市就用杭州；说了就换成他说的那个。")
    return "\n".join(lines)
