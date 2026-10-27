"""单句抽取诊断：「小雨是广州大学的学生」为什么只剩一条光秃秃的关系？

三句互补实测（`multi_sentence_probe.py`）里，第 3 句入库后只有
`对方提到的人：广州大学（学校）`，**没有"小雨是广州大学学生"这条事实**，
于是问「小雨是哪个学校的」答不出来。

本探针把这一句的**原始抽取输出**和**每一道闸门的判定**都打出来，
定位是"模型没抽出来"还是"被闸门挡掉了"。

用法：
    python tests/model_server.py run --model v1.3 --ngl 15 \
        --script tests/analysis/single_extract_probe.py
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

USER = "小雨是广州大学的学生"
REPLY = "嗯，广州大学啊。"


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"用户原话：{USER}")
    print("=" * 84)
    prompt = EX.EXTRACT_TEMPLATE.format(user=USER, reply=REPLY)
    raw = gen.complete(EX.EXTRACT_SYSTEM, prompt, max_tokens=400, temperature=0.2)
    info = EX.parse_json(raw) or {}
    print("模型原始 JSON：")
    for key in ("facts", "relations", "preferences"):
        print(f"  {key} = {info.get(key)}")

    print()
    print("=" * 84)
    print("逐条过闸门（看谁被挡、为什么）")
    print("=" * 84)
    for kind in ("facts", "relations", "preferences"):
        for content in (info.get(kind) or []):
            content = str(content)
            lines = [f"[{kind[:-1]}] {content!r}"]
            c = content
            if kind[:-1] == "relation":
                if EX._is_self_name(c):
                    lines.append("  ✗ _is_self_name：是 AI 自己的名字/自称")
                    print("\n".join(lines)); continue
                enriched = EX._enrich_relation(
                    c.replace("对方提到的人：", ""), USER)
                if enriched != c.replace("对方提到的人：", ""):
                    lines.append(f"  → _enrich_relation 补成 {enriched!r}")
                stripped = EX._strip_invented_name(
                    enriched.replace("对方提到的人：", ""), USER)
                if stripped != enriched:
                    lines.append(f"  → _strip_invented_name 降级为 {stripped!r}")
                c = EX._canonical("relation", stripped)
            else:
                c = EX._canonical(kind[:-1], c)
            g = EX._grounded(c, USER)
            a = EX._absolute_ok(c, USER)
            lines.append(f"  规范化后 = {c!r}")
            lines.append(f"  {'✅' if g else '✗'} _grounded（内容是用户说过的）")
            lines.append(f"  {'✅' if a else '✗'} _absolute_ok（没被放大）")
            print("\n".join(lines))
    print()
    print("=" * 84)
    print("结论读法：如果 facts 里根本没有这条 → **模型没抽出来**（抽取侧问题）；")
    print("          如果有但被 ✗ 挡掉 → **闸门误伤**（规则侧问题，好修）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
