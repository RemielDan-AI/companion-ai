#!/usr/bin/env python
"""工具调用循环的单元测试。**不调模型**：生成函数用桩函数注入。

覆盖：
1. 不请求工具 → 一轮结束，正文原样
2. 请求一次 → 执行 → 回填 → 第二轮出正文
3. 多工具一轮
4. **同（工具+参数）不重复执行**（防原地打转），第二次直接用缓存并告知模型
5. `max_rounds` 到顶也能收尾拿正文
6. 工具失败/抛异常都不中断对话（回填失败原因）
7. 工具被权限拒绝（blocked）→ 回填拒绝，模型可改口
8. 全部 `no_reply` → 静默收尾（不浪费时间再生成）
9. 不修改调用方传入的 messages
10. 所有分支返回值都是 ToolLoopResult，`reply` 里**不含工具块**

用法：python tests/tool_loop_test.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.protocol import END, START  # noqa: E402
from tools.tool_loop import run_loop   # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '✗'} {name}" + (f"　{detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


def blk(*lines: str) -> str:
    return "\n".join([START, *lines, END])


class ScriptedGenerate:
    """按剧本一条条返回；跑完剧本就一直返回最后一条。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def __call__(self, messages) -> str:
        self.seen.append([dict(m) for m in messages])
        if not self.replies:
            return "（剧本用完了）"
        # 逐条消耗；只剩一条时一直重复它（模拟模型固执地做同一件事）
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def main() -> int:
    print("== 1. 不请求工具 → 一轮结束 ==")
    gen = ScriptedGenerate("今天挺好，你怎么样？")
    r = run_loop(gen, [{"role": "user", "content": "在吗"}],
                 lambda n, a: {"ok": True, "text": "x"})
    check("轮数 1", r.rounds == 1, str(r.rounds))
    check("无调用", r.calls == [], str(r.calls))
    check("正文原样", r.reply == "今天挺好，你怎么样？", r.reply)
    check("stopped=done", r.stopped == "done", r.stopped)

    print("\n== 2. 请求一次 → 回填 → 第二轮出正文 ==")
    gen2 = ScriptedGenerate(
        blk("tool_name: 「始」weather「末」", "city: 「始」杭州「末」"),
        "杭州今天晴，你那边热不热？")
    used: list[tuple] = []
    r2 = run_loop(gen2, [{"role": "user", "content": "今天天气怎么样"}],
                  lambda n, a: (used.append((n, a)) or
                                {"ok": True, "text": "杭州今天晴"}))
    check("轮数 2", r2.rounds == 2, str(r2.rounds))
    check("调用了一次", used == [("weather", {"city": "杭州"})], str(used))
    check("最终正文不含工具块", START not in r2.reply and END not in r2.reply, r2.reply)
    check("正文正确", r2.reply == "杭州今天晴，你那边热不热？", r2.reply)
    check("第二次生成收到了回填", any("杭州今天晴" in m.get("content", "")
                                     for m in gen2.seen[1]),
          str([m.get("content", "")[:40] for m in gen2.seen[1]]))

    print("\n== 3. 一轮多个工具 ==")
    gen3 = ScriptedGenerate(
        blk("tool_name: 「始」weather「末」", "city: 「始」杭州「末」") + "\n" +
        blk("tool_name: 「始」recall「末」", "q: 「始」生日「末」"),
        "两样都查到了。")
    used3: list[str] = []
    r3 = run_loop(gen3, [{"role": "user", "content": "查一下"}],
                  lambda n, a: (used3.append(n) or {"ok": True, "text": f"{n}结果"}))
    check("两个工具都执行", used3 == ["weather", "recall"], str(used3))
    check("记录两条", len(r3.calls) == 2, str(len(r3.calls)))

    print("\n== 4. 同一请求不重复执行（防原地打转）==")
    same = blk("tool_name: 「始」weather「末」", "city: 「始」杭州「末」")
    gen4 = ScriptedGenerate(same, same, "好了，杭州晴。")
    used4: list[int] = []
    r4 = run_loop(gen4, [{"role": "user", "content": "天气"}],
                  lambda n, a: (used4.append(1) or {"ok": True, "text": "晴"}))
    check("工具只真执行一次", len(used4) == 1, f"执行了 {len(used4)} 次")
    # 第 1 次生成时还没有任何结果（messages 就是原始输入），所以只查第 1 轮之后。
    check("每轮都收到了结果（第 1 轮真查、之后用缓存）",
          all(any("晴" in m.get("content", "") for m in seen)
              for seen in gen4.seen[1:]),
          f"生成 {len(gen4.seen)} 次")
    # 缓存标注出现在**重复那轮**的回填里（第 1 轮还没有"重复"这回事）。
    # 不按序号断言（生成次数会随 max_rounds 而变），改看是否存在。
    check("缓存命中被标注",
          any("同一个请求" in m.get("content", "")
              for seen in gen4.seen for m in seen),
          "回填里应说明这次是复用上次结果，别让模型以为又查了一次")
    check("最终仍出了正文", r4.reply == "好了，杭州晴。", r4.reply)

    print("\n== 5. max_rounds 到顶也能收尾 ==")
    loop_forever = blk("tool_name: 「始」t「末」", "i: 「始」1「末」")
    gen5 = ScriptedGenerate(loop_forever, loop_forever, loop_forever,
                            loop_forever, "收尾正文")
    r5 = run_loop(gen5, [{"role": "user", "content": "x"}],
                  lambda n, a: {"ok": True, "text": "y"}, max_rounds=2)
    check("stopped=max_rounds", r5.stopped == "max_rounds", r5.stopped)
    check("轮数不超过上限", r5.rounds <= 2, str(r5.rounds))

    # 模型固执地一直请求工具（剧本只有工具块，永远不给正文）：
    # 这时**绝不能把工具块当正文返回**，否则用户会看到 <<<[TOOL_REQUEST]>>> 乱码。
    stubborn = ScriptedGenerate(loop_forever)
    r5b = run_loop(stubborn, [{"role": "user", "content": "x"}],
                   lambda n, a: {"ok": True, "text": "y"}, max_rounds=2)
    check("固执请求工具时不泄漏协议标记",
          START not in r5b.reply and END not in r5b.reply, repr(r5b.reply[:60]))
    check("stopped 仍是 max_rounds", r5b.stopped == "max_rounds", r5b.stopped)
    check("确实去催过它（多跑了收尾生成）", len(stubborn.seen) >= 3,
          f"generate 被调 {len(stubborn.seen)} 次")
    # 能在上限内拿到正文时，应当拿到正文
    gen5c = ScriptedGenerate(loop_forever, loop_forever, "好的，我直接说。")
    r5c = run_loop(gen5c, [{"role": "user", "content": "x"}],
                   lambda n, a: {"ok": True, "text": "y"}, max_rounds=1)
    check("能拿到正文时优先用正文", r5c.reply == "好的，我直接说。", repr(r5c.reply))

    print("\n== 6. 工具失败/抛异常都不中断 ==")
    gen6 = ScriptedGenerate(
        blk("tool_name: 「始」boom「末」"), "抱歉，那个查不到。")
    r6 = run_loop(gen6, [{"role": "user", "content": "x"}],
                  lambda n, a: (_ for _ in ()).throw(RuntimeError("工具炸了")))
    check("异常被兜住且继续", r6.reply == "抱歉，那个查不到。", r6.reply)
    check("失败被记录", r6.calls and r6.calls[0]["ok"] is False, str(r6.calls))
    check("失败原因传给了模型",
          any("工具炸了" in m.get("content", "") for m in gen6.seen[1]),
          str([m.get("content", "")[:60] for m in gen6.seen[1]]))

    gen6b = ScriptedGenerate(blk("tool_name: 「始」t「末」"), "没查到。")
    r6b = run_loop(gen6b, [{"role": "user", "content": "x"}],
                   lambda n, a: {"ok": False, "text": "", "detail": "超时"})
    check("返回 ok=False 也继续", r6b.reply == "没查到。", r6b.reply)
    check("detail 被回填", any("超时" in m.get("content", "") for m in gen6b.seen[1]))

    print("\n== 7. 权限拒绝 → 模型可改口 ==")
    gen7 = ScriptedGenerate(blk("tool_name: 「始」danger「末」"),
                            "那个我不能做，但可以陪你聊别的。")
    r7 = run_loop(gen7, [{"role": "user", "content": "帮我干点坏事"}],
                  lambda n, a: {"ok": False, "text": "",
                                "detail": "权限「local_exec」未开启"})
    check("被拒后仍出正文", "不能做" in r7.reply, r7.reply)
    check("拒绝原因回填",
          any("未开启" in m.get("content", "") for m in gen7.seen[1]),
          str([m.get("content", "")[:60] for m in gen7.seen[1]]))

    print("\n== 8. 全部 no_reply → 静默收尾 ==")
    gen8 = ScriptedGenerate(blk("tool_name: 「始」mark「末」",
                                "no_reply: 「始」true「末」"))
    used8: list[int] = []
    r8 = run_loop(gen8, [{"role": "user", "content": "x"}],
                  lambda n, a: (used8.append(1) or {"ok": True, "text": "已记"}))
    check("工具执行了", len(used8) == 1, str(len(used8)))
    check("不再多跑一轮生成", len(gen8.seen) == 1,
          f"generate 被调 {len(gen8.seen)} 次")
    check("reply 为空", r8.reply == "", repr(r8.reply))
    check("stopped=done", r8.stopped == "done", r8.stopped)

    print("\n== 9. 不修改传入的 messages ==")
    original = [{"role": "user", "content": "在吗"}]
    snapshot = [dict(m) for m in original]
    gen9 = ScriptedGenerate(blk("tool_name: 「始」t「末」"), "好了")
    run_loop(gen9, original, lambda n, a: {"ok": True, "text": "y"})
    check("调用方列表没被改动", original == snapshot, str(original))

    print("\n== 10. 返回值一致性 ==")
    gen10 = ScriptedGenerate("<thinking>没闭合\n" + blk("tool_name: 「始」x「末」"))
    r10 = run_loop(gen10, [{"role": "user", "content": "x"}],
                   lambda n, a: {"ok": True, "text": "y"})
    check("未闭合思考区不触发执行", r10.calls == [], str(r10.calls))
    check("返回值是 ToolLoopResult",
          type(r10).__name__ == "ToolLoopResult", type(r10).__name__)
    check("reply 不含 START 标记", START not in r10.reply, repr(r10.reply[:40]))

    print()
    if FAIL:
        print(f"✗ {len(FAIL)} 项未通过：" + "、".join(FAIL))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
