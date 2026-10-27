#!/usr/bin/env python
"""「模型自选工具」接入 `app.py` 的集成测试。

**不连真模型、不联网**：
- 模型那一层：把 `Companion.generator._post` 换成一个假生成器（返回工具块或正文）；
- 网络那一层：只打桩 `tools.builtin._get_json`（地理编码/天气数据）；
- 其余**全走真实代码**：技能路由、`needs_model_tools` 决策、`tools_prompt` 拼提示、
  `run_loop`、`build_runner`、`tools/executor.run_tool` 的权限检查、结果回填、收尾。

为什么值得这么测：接线的价值全在「这些零件连起来到底会不会动」，
而单测已经覆盖了各零件自身。

用法：python tests/tool_loop_wiring_test.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.protocol import END, START  # noqa: E402
from tools import builtin as builtin_tool  # noqa: E402
from cognition import linkage  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "toolwiring"
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '✗'} {name}" + (f"　{detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


def blk(*lines: str) -> str:
    return "\n".join([START, *lines, END])


class FakePost:
    """替掉 generator._post：**只拦人设对话**，按剧本返回；记录收到的 messages。

    为什么必须区分：`generator._post` 不只有对话在用——每轮之后的**记忆提取**
    也走它（system 提示是「你是记忆整理器…」）。如果无差别打桩，
    提取调用会被喂成工具块，测试就会看到一堆莫名其妙的额外"轮数"。
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []
        self.other: list[list[dict]] = []
        self._real = None            # 由 install() 填真实的 _post

    def install(self, generator) -> None:
        self._real = generator._post   # 拿的就是绑定好的方法
        generator._post = self

    def _is_chat(self, messages) -> bool:
        first = (messages or [{}])[0]
        content = str(first.get("content") or "")
        return first.get("role") == "system" and "未央" in content

    def __call__(self, messages, params):
        if not self._is_chat(messages):
            self.other.append([dict(m) for m in messages])
            if self._real is not None:
                return self._real(messages, params)
            return ""
        self.seen.append([dict(m) for m in messages])
        if not self.replies:
            return "（剧本用完了）"
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def stub_network(monkey_urls: dict) -> None:
    """打桩 tools.builtin._get_json：按 URL 关键字返回假数据。"""
    def fake(url, timeout=8):
        if "geocoding" in url:
            return {"results": [{"name": monkey_urls.get("city", "杭州"),
                                 "latitude": 30.25, "longitude": 120.16}]}
        return {"current": {"temperature_2m": 24.5, "relative_humidity_2m": 60,
                            "apparent_temperature": 26.0, "weather_code": 1,
                            "wind_speed_10m": 8.0}}
    builtin_tool._get_json = fake


def make_companion(constructor):
    tmp = TMP
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return constructor(memory_path=tmp / "m.db", index_path=tmp / "i.json",
                       profile_path=tmp / "p.json", values_path=tmp / "v.yaml",
                       scenarios_path=tmp / "sc.jsonl",
                       preferences_path=tmp / "pf.jsonl",
                       ai_profile_path=tmp / "ai.json")


def main() -> int:
    import app as app_module
    Companion = app_module.Companion

    print("== 0. 准备 ==")
    stub_network({"city": "杭州"})
    comp = make_companion(Companion)
    tools = comp._skill_list()
    check("_skill_list 拿到了技能", len(tools) > 0, f"{len(tools)} 个")
    collected = app_module.collect_tools(tools)
    check("collect_tools 只暴露只读的 weather",
          [t["name"] for t in collected] == ["weather"],
          str([t["name"] for t in collected]))
    check("weather 带完整工具定义（权限检查要用）",
          "_def" in (collected[0] if collected else {}),
          str(list((collected[0] if collected else {}).keys())))

    print("\n== 1. 实时信息线索 → 会挂工具说明 ==")
    fake = FakePost(blk("tool_name: 「始」weather「末」", "city: 「始」杭州「末」"),
                    "杭州今天晴，24 度上下，挺舒服的。")
    fake.install(comp.generator)
    reply, _ = comp.say("外面冷吗")
    check("最终回复是正文", reply == "杭州今天晴，24 度上下，挺舒服的。", reply)
    check("走了工具循环（跑了 2 轮生成）", len(fake.seen) == 2, f"{len(fake.seen)} 轮")
    check("第 1 轮的 system 提示里有工具说明",
          any("TOOL_REQUEST" in m.get("content", "") and m.get("role") == "system"
              for m in fake.seen[0]),
          "工具说明应拼进 system")
    check("第 2 轮收到了工具结果",
          any("杭州" in m.get("content", "") and "工具" in m.get("content", "")
              for m in fake.seen[1]),
          str([m.get("content", "")[:60] for m in fake.seen[1]]))
    check("回复里没有协议标记", START not in reply and END not in reply, reply)
    loop = getattr(comp, "last_tool_loop", None)
    check("记录了工具调用", loop is not None and len(loop.calls) == 1,
          str(loop.calls if loop else None))
    check("工具真的执行成功", loop is not None and loop.calls[0]["ok"] is True,
          str(loop.calls[0] if loop else None))
    check("工具结果里带城市名（老问题：不许省地点）",
          loop is not None and "杭州" in loop.calls[0]["text"],
          str(loop.calls[0]["text"][:50] if loop else None))
    # pending_skill 在 _finish（写技能日志）时就被消费清空了，所以要看 last_tool_loop
    check("工具调用被记录在 last_tool_loop",
          getattr(comp, "last_tool_loop", None) is not None
          and len(comp.last_tool_loop.calls) == 1,
          str(getattr(comp, "last_tool_loop", None)))
    check("技能日志记到了模型自选工具",
          len([x for x in linkage.skill_logs(comp.memory, limit=5)
               if str(x.get("skill_id")) == "model_choice"]) == 1,
          str([{k: x.get(k) for k in ("skill_id", "tool", "detail", "ok")}
               for x in linkage.skill_logs(comp.memory, limit=5)]))

    print("\n== 2. 没有实时线索 → 不挂工具说明（省提示词） ==")
    comp2 = make_companion(Companion)
    fake2 = FakePost("嗯，我在听。")
    fake2.install(comp2.generator)
    reply2, _ = comp2.say("我今天有点累")
    check("回复正常", reply2 == "嗯，我在听。", reply2)
    check("只跑了 1 轮", len(fake2.seen) == 1, f"{len(fake2.seen)} 轮")
    check("system 提示里没有工具说明",
          not any("TOOL_REQUEST" in m.get("content", "")
                  for m in fake2.seen[0]),
          "非实时问题不该塞工具说明")

    print("\n== 3. 模型不请求工具 → 退回普通回答（不破坏原行为） ==")
    comp3 = make_companion(Companion)
    fake3 = FakePost("今天挺暖和的，你那边呢？")
    fake3.install(comp3.generator)
    reply3, _ = comp3.say("外面冷吗")
    check("直接出正文", reply3 == "今天挺暖和的，你那边呢？", reply3)
    check("只跑了 1 轮", len(fake3.seen) == 1, f"{len(fake3.seen)} 轮")
    # 模型没请求工具 → 循环跑了一轮、没有调用记录
    loop3 = getattr(comp3, "last_tool_loop", None)
    check("循环跑过但没有工具调用",
          loop3 is not None and loop3.calls == [],
          str(loop3.calls if loop3 else None))

    print("\n== 4. 工具失败 → 回填原因，仍有正文 ==")
    comp4 = make_companion(Companion)
    builtin_tool._get_json = lambda url, timeout=8: (_ for _ in ()).throw(
        RuntimeError("网络炸了"))
    fake4 = FakePost(blk("tool_name: 「始」weather「末」", "city: 「始」杭州「末」"),
                     "天气我这儿没查到，网好像不太行。")
    fake4.install(comp4.generator)
    reply4, _ = comp4.say("外面冷吗")
    check("失败后仍出正文", "没查到" in reply4, reply4)
    check("失败原因回填给了模型",
          any("网络炸了" in m.get("content", "") for m in fake4.seen[1]),
          str([m.get("content", "")[:70] for m in fake4.seen[1]]))
    loop4 = getattr(comp4, "last_tool_loop", None)
    check("失败被记为 ok=False", loop4 is not None and loop4.calls[0]["ok"] is False,
          str(loop4.calls[0] if loop4 else None))
    stub_network({"city": "杭州"})      # 还原

    print("\n== 5. 模型要一个不存在的工具 → 明确拒绝并说明可用的 ==")
    comp5 = make_companion(Companion)
    fake5 = FakePost(blk("tool_name: 「始」rm_rf「末」"), "那个我做不了。")
    fake5.install(comp5.generator)
    reply5, _ = comp5.say("现在几度")
    check("仍出正文", reply5 == "那个我做不了。", reply5)
    check("回填里说明没有这个工具且列出可用的",
          any("没有这个工具" in m.get("content", "")
              and "weather" in m.get("content", "") for m in fake5.seen[1]),
          str([m.get("content", "")[:80] for m in fake5.seen[1]]))

    print("\n== 6. 中间往返不进短期上下文（防污染） ==")
    comp6 = make_companion(Companion)
    fake6 = FakePost(blk("tool_name: 「始」weather「末」", "city: 「始」杭州「末」"),
                     "杭州晴。")
    fake6.install(comp6.generator)
    comp6.say("外面冷吗")
    history = comp6.generator.history
    check("历史里只有一问一答", len(history) == 2, f"{len(history)} 条")
    check("历史里的回复是最终正文", history and history[-1]["content"] == "杭州晴。",
          str(history[-1] if history else None))
    check("历史里没有工具块",
          not any(START in (m.get("content") or "") for m in history),
          str([m.get("content") or "" for m in history]))

    print("\n== 7. 工具说明只挂在末尾问题之外（不放进历史） ==")
    check("第 2 轮 messages 仍以 user 结尾",
          fake6.seen[1][-1]["role"] == "user",
          str(fake6.seen[1][-1]["role"]))

    print()
    if FAIL:
        print(f"✗ {len(FAIL)} 项未通过：" + "、".join(FAIL))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
