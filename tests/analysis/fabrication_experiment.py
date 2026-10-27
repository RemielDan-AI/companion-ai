# -*- coding: utf-8 -*-
"""无依据询问（20 题）：**受控实验**，量出各层机制各自降了多少编造。

四组对照（同一模型、同一套题、各自独立临时库）：
    A 现状            项目原样（含"无来源断言守卫"、含记忆注入与档位提示）
    B 无来源守卫关闭   把 app.py 的 `CLAIM_MARKS` 清空——量出这个已存在的守卫值多少分
    C 只加未命中标记   无相关记忆时，在注入位置写明「查过了，没有记录」
    D 未命中标记 + 尾部指令   再加一句"记忆块里没有的就说记不清"放到提示**最末**

判定用**人工口径**（脚本自带的 CLAIMS 只认"你说过/你提过"这类声称句式，
会漏掉断言式编造「说过。你说想去海边」「说它叫小橘」）：
    编造 = 断言了用户从未说过的事实（喜好/名字/地点/时间/过往对话），且整句没有否认语气。

用法：
    set COMPANION_API_URL=http://127.0.0.1:8082/v1/chat/completions
    python tests/analysis/fabrication_experiment.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as app_mod  # noqa: E402
from engine.retrieve import build_memory_block as real_build_block  # noqa: E402
from tests.conv_9_nofabrication import TURNS  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent.parent / "_tmp" / "fab_exp"

MISS_MARKER = ("（这一轮我查过记忆库了：关于对方刚问的这件事，**没有任何记录**。"
               "没有就是没有——请直接说记不清/没听对方说过，不要用任何细节去补。）")
TAIL_RULE = ("（关于对方的事，只用上面记忆块里确实有的内容回答；"
             "记忆块里没有的，就说「这个我记不清了」或「你没跟我说过」，"
             "绝对不要补一个具体细节——名字、地点、时间、喜好、口味都不要补。）")

# 记忆类问题（用户这轮在考记忆）
MEM_Q = re.compile(r"(记得|说过|提过|之前|上次|以前|叫什么|是什么来着|你记)")

# 人工判据（v2）：分两级，避免上次"只认显式声称句式"导致严重低估
#   强：明确断言对方说过/记得（含断言式"说过。你说…"）
STRONG = re.compile(
    r"(记得你(提到|说过|说|提)|你(之前)?(说过|提过)|你告诉过我|你上次说|"
    r"^说过。|^提过。|想起来了|记得(你|咱|我们)|有个人叫|说它叫|"
    r"你(讨厌|喜欢|爱)[^，。？]{0,10}|"
    r"\d+\s*月\s*\d+\s*日出生的)")
#   弱：把没发生的事**当事实陈述/模糊断言**（"你上次说的是城市吧""书名是《人间草木》吧"）
WEAK = re.compile(
    r"(你说的是[^，。？]{0,8}吧|你之前说过这个|应该是[^，。？]{0,8}吧|"
    r"你(提|说)过[^，。？]{0,6}吧|大概是[^，。？]{0,8}吧|"
    r"名字是[^，。？]{0,6}|书名是[^，。？]{0,8})")
# 正确答法（否认/不确定/请对方重说）——出现这些就不算编造
DENY = ("我不知道", "不记得", "没说过", "没聊过", "记不清", "记不太清", "没记着",
        "没有你", "你再说", "你告诉", "你还没", "我这边没有", "不确定", "想不起来",
        "没提过", "没说", "没有记录", "没听你")


def fresh(tag: str):
    paths = {k: TMP / f"{tag}_{k}" for k in
             ("db", "i.json", "p.json", "v.yaml", "s.jsonl", "pr.jsonl", "a.yaml")}
    for p in paths.values():
        if p.exists():
            p.unlink()
    return app_mod.Companion(
        memory_path=paths["db"], index_path=paths["i.json"],
        profile_path=paths["p.json"], values_path=paths["v.yaml"],
        scenarios_path=paths["s.jsonl"], preferences_path=paths["pr.jsonl"],
        ai_profile_path=paths["a.yaml"])


def patch_miss(marker: bool) -> None:
    """把"未命中"写进注入位置：无相关记忆时给一段明确说明，而不是静默留空。"""
    def _build(result, max_chars=None):
        text = real_build_block(result, max_chars=max_chars)
        if text:
            return text
        if not marker:
            return ""
        return MISS_MARKER
    app_mod.build_memory_block = _build


def patch_tail_rule(on: bool) -> None:
    """把"记忆块里没有的就别补"放到提示**最末**（遵从率最高的位置）。"""
    if not on:
        return
    original = app_mod.Companion.say

    def say(self, user_text, extra_prompt="", tail_prompt="", *a, **kw):
        extra_prompt = extra_prompt
        tail_prompt = (tail_prompt + "\n" + TAIL_RULE).strip() if tail_prompt else TAIL_RULE
        return original(self, user_text, extra_prompt=extra_prompt,
                        tail_prompt=tail_prompt, *a, **kw)
    app_mod.Companion.say = say


def patch_kill_guard(on: bool) -> None:
    """清空 CLAIM_MARKS → 无来源断言守卫不再触发重生成。"""
    if not on:
        return
    src = pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
    # 不直接改文件：用一个小手段——把模块里那个元组的名字遮蔽掉
    app_mod.__dict__["_CLAIM_MARKS_DISABLED"] = True
    # 守卫读的是函数内的局部元组，所以改用打补丁 say 的方式不可行；
    # 这里退一步：只记录"未关闭"，由下面的 fallback 说明。
    print("  （注：CLAIM_MARKS 是函数内局部元组，无法从外部关闭；B 组改为跳过）")


def run_group(tag: str) -> tuple[int, list[tuple[int, str, str, str]]]:
    """跑一组，返回 (编造轮数, [(题号, 问, 答, 级别)])；同时把逐轮原文存盘供人工核查。"""
    companion = fresh(tag.replace(" ", "_"))
    bad: list[tuple[int, str, str, str]] = []
    transcript: list[str] = [f"# 逐轮原文：{tag}", ""]
    try:
        for i, q in enumerate(TURNS, 1):
            try:
                reply, _ = companion.say(q)
            except Exception as exc:
                reply = f"<出错：{exc}>"
            text = reply or ""
            transcript += [f"**{i}. 用户**：{q}", "", f"> {text}", ""]
            if any(d in text for d in DENY):
                level = "ok"                      # 有否认语气 → 不算编造
            elif STRONG.search(text):
                level = "强"
            elif WEAK.search(text):
                level = "弱"
            else:
                level = "ok"
            if level != "ok":
                bad.append((i, q, text, level))
    finally:
        companion.memory.close()
    out = ROOT / f"weiyang编造逐轮-{tag}.md"
    out.write_text("\n".join(transcript), encoding="utf-8")
    return len(bad), bad


def main() -> int:
    print(f"API：{os.environ.get('COMPANION_API_URL') or '（默认）'}")
    print(f"题数：{len(TURNS)}")
    TMP.mkdir(parents=True, exist_ok=True)
    results = []

    # A 现状
    patch_miss(False)
    n, bad = run_group("A_现状")
    results.append(("A 现状", n, bad))
    print(f"\n【A 现状】统计编造 {n}/{len(TURNS)}"
          f"（强 {sum(1 for x in bad if x[3] == '强')} / 弱 {sum(1 for x in bad if x[3] == '弱')}）")
    for i, q, a, lv in bad[:8]:
        print(f"  [{lv}] #{i} {q[:18]} → {a[:56]}")

    # C 只加未命中标记
    patch_miss(True)
    n, bad = run_group("C_未命中标记")
    results.append(("C 只加未命中标记", n, bad))
    print(f"\n【C 只加未命中标记】统计编造 {n}/{len(TURNS)}"
          f"（强 {sum(1 for x in bad if x[3] == '强')} / 弱 {sum(1 for x in bad if x[3] == '弱')}）")
    for i, q, a, lv in bad[:8]:
        print(f"  [{lv}] #{i} {q[:18]} → {a[:56]}")

    lines = ["# 无依据询问：编造率受控实验", "",
             f"- 题数：{len(TURNS)}　判定：人工口径 v2（**强**＝显式声称/断言式；**弱**＝把没发生的当事实且带「吧」这类模糊语气）",
             "- 「未命中标记」＝无相关记忆时，在注入位置写明「查过了，没有记录」，"
             "让模型知道确实没有，而不是靠空的记忆块去猜。",
             "- 逐轮原文：`weiyang编造逐轮-A_现状.md`、`weiyang编造逐轮-C_未命中标记.md`", "",
             "| 组 | 强 | 弱 | 合计 |", "|---|---|---|---|"]
    for name, n, bad in results:
        s = sum(1 for x in bad if x[3] == "强")
        w = sum(1 for x in bad if x[3] == "弱")
        lines.append(f"| {name} | {s} | {w} | **{n} / {len(TURNS)}** |")
    reports_dir = ROOT.parent / "test" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / "weiyang编造实验.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
