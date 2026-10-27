#!/usr/bin/env python
"""抽取质量横向对照（多模型同一套题，只对"本句明确说了名字"的用例打分）。

## 背景

抽取走的是**聊天 LLM**（`generator.complete` → chat completions），
**嵌入模型完全不参与**。而抽取与聊天**共用同一套权重**，
微调数据里又没有抽取样本（`weiyang_train.jsonl` 1085 条里 0 条），
所以微调版会把名字抽坏（实测：「林小雨」→「林小记得」）。

本探针用来横向比：官方 4B / 微调 4B / 各档小模型，谁适合当**记忆抽取器**。

## 判据为什么这么定（上一版踩过）

上一版把所有用例都按"原话没有名字就不该造"打分，**过严**：
同一进程里服务端 slot 复用会把**前面轮次已确立的实体**带进来，
「我妹妹对花生过敏」承接出「林小雨」其实是**正确**行为。
所以现在：
    · **打分的用例**：本句明确说了名字 → 要求抽出的名字**与所说完全一致**
    · **只观察不打分**：本句没名字 → 记录它产出了什么（承接还是编造，人工看）
    · 用例顺序固定且各模型一致 → slot 复用的影响对每个模型是**同等**的，比较仍公平

用法：
    python tests/model_server.py run --model <关键词> --ngl 99 \
        --script tests/analysis/extract_quality_probe.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

# (用户原话, 未央回答, 本句所说的名字)　名字取**互不相同**的，避免互相干扰
SCORED = [
    ("我妹妹叫林小雨", "林小雨，我记住了。", "林小雨"),
    ("我同事叫陈思远，做后端的", "陈思远，记下了。", "陈思远"),
    ("我养了只狗叫阿黄", "阿黄，好名字。", "阿黄"),
    ("我大学室友叫王小雅", "王小雅，记住了。", "王小雅"),
    ("我爸爸叫赵国强", "赵国强，我记下了。", "赵国强"),
    ("我家猫叫雪球，三岁了", "雪球三岁啦。", "雪球"),
    ("我外甥叫李一鸣，上小学", "李一鸣上小学啊。", "李一鸣"),
    ("我邻居叫周惠敏", "周惠敏，记住了。", "周惠敏"),
]

# 只观察不打分：本句没有名字，看它产出了什么（承接上文属正常）
OBSERVE = [
    ("我妹妹对花生过敏", "花生我记下了。"),
    ("我公司下个月要搬去滨江", "搬到滨江，通勤会变吗？"),
    ("我最近在学吉他", "学吉他好啊。"),
]


def names_in(text) -> list[str]:
    return [m.group(1) for m in
            re.finditer(r"([\u4e00-\u9fff]{2,12})（([^）]+)）", str(text or ""))]


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    tag = os.environ.get("EXTRACT_MODEL_TAG", "")
    print(f"抽取质量对照　模型标签：{tag or '(未标)'}　服务：{url}")
    print("=" * 92)

    exact = same_turn = 0
    for user, reply, want in SCORED:
        info = EX.extract_turn(gen, user, reply)
        rels = info.get("relations") or []
        got = names_in(" ".join(map(str, rels)))
        ok = (want in got) and all(g == want for g in got if g)
        exact += ok
        if want in got:
            same_turn += 1
        print(f"{'✅' if ok else '✗'} 用户：{user}")
        print(f"     relations = {rels}")
        if not ok:
            print(f"     期望「{want}」，实得 {got}")
    n = len(SCORED)
    print("-" * 92)
    print(f"**名字完全保真：{exact}/{n}**　（其中至少含正确名字：{same_turn}/{n}）")

    print()
    print("观察项（本句没有名字，不打分）")
    print("-" * 92)
    for user, reply in OBSERVE:
        info = EX.extract_turn(gen, user, reply)
        rels = info.get("relations") or []
        facts = info.get("facts") or []
        print(f"· 用户：{user}")
        print(f"     relations = {rels}")
        print(f"     facts     = {[str(f)[:36] for f in facts]}")
    print("=" * 92)
    print(f"（口径：抽取看聊天 LLM，不看嵌入模型；名字保真 {exact}/{n}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
