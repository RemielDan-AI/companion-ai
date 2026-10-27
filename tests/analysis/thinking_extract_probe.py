#!/usr/bin/env python
"""看**推理模型**（如 DeepSeek-R1-Distill）做抽取时的原始输出。

## 为什么要单独看

R1-Distill 这类模型会先吐 ` thinking…<｜end▁of▁thinking｜>` 思维链。
而 `extract_turn()` 用的是 `max_tokens=400`、`parse_json()` 靠
`raw.find("{") … raw.rfind("}")` 取第一个 JSON 对象。两个风险：

    ① 思维链把 400 token 吃光 → JSON 根本没生成 → 抽取为空
    ② 思维链里出现花括号 → `find("{")` 抓到**思维链里的**花括号 → 解析出垃圾

（`tools/protocol.py: strip_reasoning()` 能处理"没闭合的 think"，
但 `cognition/extract.py` **没有调用它**——这是本探针要确认的。）

用法：
    python tests/model_server.py run --model <关键词> --ngl 99 \
        --script tests/analysis/thinking_extract_probe.py
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

CASES = [
    ("我妹妹叫林小雨", "林小雨，我记住了。"),
    ("我同事叫陈思远，做后端的", "陈思远，记下了。"),
]


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    tag = os.environ.get("EXTRACT_MODEL_TAG", "")
    print(f"原始输出诊断　模型：{tag or '(未标)'}")
    print("=" * 88)
    for user, reply in CASES:
        prompt = EX.EXTRACT_TEMPLATE.format(user=user, reply=reply)
        raw = gen.complete(EX.EXTRACT_SYSTEM, prompt, max_tokens=400, temperature=0.2)
        parsed = EX.parse_json(raw) or {}
        has_think = "think" in raw.lower()
        print(f"用户：{user}")
        print(f"  原始输出 {len(raw)} 字；含 think 标记={has_think}")
        print(f"  前 260 字：{raw[:260]!r}")
        print(f"  末尾 120 字：{raw[-120:]!r}")
        print(f"  parse_json → relations={parsed.get('relations')!r} "
              f"facts={parsed.get('facts')!r}")
        if not parsed:
            print("  ⚠ **解析失败/空**：思维链可能吃光了预算，或花括号被抓错")
        print("-" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
