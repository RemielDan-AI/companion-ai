#!/usr/bin/env python
"""工具调用协议（中文定界符）的单元测试。不调模型、不碰真实数据。

覆盖：
1. 正常解析（单块、多块、多参数）
2. 宽松写法（漏结束标记、大小写、多余空白）
3. **多行值 + ESCAPE 转义区**（转义区里的「末」不算块结束）
4. **未闭合思考区 → 后面全丢**（安全取向）
5. 未闭合工具块 → 丢弃
6. 元字段不混进 args；no_reply / mark_history 生效
7. max_calls 上限
8. 脏输入不崩（None / 空 / 纯文本 / 只有一半标记）
9. tools_prompt 生成的样板能被自己的 parse 解析回来（**自洽性**）

第 9 条最关键：提示里给的样板如果自己都解析不了，模型学得再对也没用。

用法：python tests/tool_protocol_test.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.protocol import (END, START, ToolRequest, format_result,  # noqa: E402
                           parse, strip_reasoning, tools_prompt)

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '✗'} {name}" + (f"　{detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


def block(*lines: str) -> str:
    return "\n".join([START, *lines, END])


def main() -> int:
    print("== 1. 正常解析 ==")
    r = parse(block("tool_name: 「始」weather「末」", "city: 「始」杭州「末」"))
    check("解析出 1 个请求", len(r) == 1, f"got {len(r)}")
    if r:
        check("工具名正确", r[0].name == "weather", r[0].name)
        check("参数正确", r[0].args == {"city": "杭州"}, str(r[0].args))
        check("默认非静默", r[0].no_reply is False)

    multi = parse(block("tool_name: 「始」a「末」") + "\n" + block("tool_name: 「始」b「末」"))
    check("多个块都解析出来", [x.name for x in multi] == ["a", "b"],
          str([x.name for x in multi]))

    two_args = parse(block("tool_name: 「始」search「末」", "q: 「始」天气「末」",
                           "limit: 「始」3「末」"))
    check("两个参数", two_args and two_args[0].args == {"q": "天气", "limit": "3"},
          str(two_args[0].args if two_args else None))

    print("\n== 2. 宽松写法 ==")
    loose = parse(block("tool_name: 「始」weather「末」", "city: 杭州"))
    check("漏结束标记时按行取值", loose and loose[0].args.get("city") == "杭州",
          str(loose[0].args if loose else None))
    upper = parse(block("TOOL_NAME: 「始」weather「末」"))
    check("键名大小写不敏感", upper and upper[0].name == "weather",
          str(upper[0].name if upper else None))
    spaced = parse(block("  tool_name :  「始」 weather 「末」 "))
    check("多余空白被容忍", spaced and spaced[0].name == "weather",
          repr(spaced[0].name) if spaced else None)

    print("\n== 3. 多行值 + ESCAPE 转义区 ==")
    esc = (f"{START}\n"
           f"tool_name: 「始」write_note「末」\n"
           f"text: 「始ESCAPE」第一行\n"
           f"第二行含「末」和 {END} 字样\n"
           f"第三行「末ESCAPE」\n"
           f"{END}")
    re_ = parse(esc)
    check("转义区内的「末」/结束标记不截断块", len(re_) == 1, f"got {len(re_)}")
    if re_:
        val = re_[0].args.get("text", "")
        check("多行内容完整保留", "第一行" in val and "第二行" in val and "第三行" in val,
              repr(val[:40]))
        check("ESCAPE 标记本身被剥掉",
              "ESCAPE" not in val and "「末」" in val, repr(val[:40]))

    print("\n== 4. 未闭合思考区 → 后面全丢（安全取向）==")
    leaked = "正常开头\n<thinking>我该查一下天气……\n" + block("tool_name: 「始」weather「末」")
    check("未闭合思考区之后的工具块被丢弃", parse(leaked) == [],
          f"got {[x.name for x in parse(leaked)]}")
    closed = ("开头\n<thinking>想一下</thinking>\n" + block("tool_name: 「始」weather「末」"))
    check("已闭合思考区之后的工具块正常解析",
          [x.name for x in parse(closed)] == ["weather"],
          str([x.name for x in parse(closed)]))
    check("strip_reasoning 直接可用",
          strip_reasoning("a<thinking>x</thinking>b") == "ab",
          repr(strip_reasoning("a<thinking>x</thinking>b")))

    print("\n== 5. 未闭合工具块 → 丢弃 ==")
    check("只有开始标记", parse(f"{START}\ntool_name: 「始」weather「末」") == [])
    check("块没闭合但后面还有内容",
          [x.name for x in parse(f"{START}\ntool_name: 「始」a「末」\n乱")] == [],
          str([x.name for x in parse(f"{START}\ntool_name: 「始」a「末」\n乱")]))

    print("\n== 6. 元字段与开关 ==")
    meta = parse(block("tool_name: 「始」weather「末」",
                       "no_reply: 「始」true「末」",
                       "mark_history: 「始」true「末」"))
    check("no_reply 生效", meta and meta[0].no_reply is True)
    check("mark_history 生效", meta and meta[0].mark_history is True)
    check("元字段不进 args", meta and "no_reply" not in meta[0].args
          and "mark_history" not in meta[0].args, str(meta[0].args if meta else None))
    check("tool_name 不进 args", meta and "tool_name" not in meta[0].args)
    no_name = parse(block("city: 「始」杭州「末」"))
    check("没有 tool_name 的块被丢弃", no_name == [], f"got {len(no_name)}")

    print("\n== 7. max_calls 上限 ==")
    many = "\n".join(block(f"tool_name: 「始」t{i}「末」") for i in range(10))
    check("默认最多 4 个", len(parse(many)) == 4, f"got {len(parse(many))}")
    check("可调上限", len(parse(many, max_calls=2)) == 2,
          f"got {len(parse(many, max_calls=2))}")

    print("\n== 8. 脏输入不崩 ==")
    for dirty in (None, "", "   ", "普通聊天，没有任何工具块",
                  START, END, "「始」孤立的「末」",
                  "<<<[TOOL_REQUEST]>>><<<[END_TOOL_REQUEST]>>>"):
        try:
            parse(dirty)  # type: ignore[arg-type]
            ok = True
        except Exception as exc:
            ok = False
            print(f"     崩在 {dirty!r}: {exc}")
        check(f"不崩：{str(dirty)[:28]!r}", ok)

    print("\n== 9. 自洽性：tools_prompt 的样板能被 parse 解析回来 ==")
    tools = [{"name": "weather", "desc": "查天气", "args": ["city"]},
             {"name": "recall", "desc": "查记忆", "args": []}]
    prompt = tools_prompt(tools)
    check("提示里含工具清单", "weather" in prompt and "recall" in prompt)
    parsed = parse(prompt)
    check("提示里的块都能解析", len(parsed) >= 1, f"got {len(parsed)}")
    names = [x.name for x in parsed]
    # 提示里有两个块：第一个是「格式说明」用的，工具名是占位文字；
    # **第二个必须是可直接执行的真样板**（真实工具名 + 真实参数名）。
    # 最初我只放了一个占位样板，模型照抄就会请求一个叫"工具名"的工具——
    # 这条断言就是为了钉住那个缺陷。
    check("存在可直接执行的样板（真实工具名）", "weather" in names, str(names))
    ex = next((x for x in parsed if x.name == "weather"), None)
    check("样板带真实参数名", ex is not None and "city" in ex.args,
          str(ex.args if ex else None))
    check("样板参数值不是占位符", ex is not None and ex.args.get("city") not in
          ("这里填值", "对应参数值", "参数值"), str(ex.args if ex else None))
    check("空工具表返回空串", tools_prompt([]) == "")

    print("\n== 10. format_result ==")
    ok_line = format_result("weather", True, "杭州今天晴")
    check("成功结果带工具名与内容",
          "weather" in ok_line and "成功" in ok_line and "杭州今天晴" in ok_line,
          ok_line)
    bad_line = format_result("weather", False, "超时")
    check("失败结果标注失败", "失败" in bad_line, bad_line)

    print()
    if FAIL:
        print(f"✗ {len(FAIL)} 项未通过：" + "、".join(FAIL))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
