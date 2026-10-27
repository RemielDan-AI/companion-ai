#!/usr/bin/env python
"""阶段 4 验收测试：深度路由 + 风格引擎 + 后处理。

分三部分：
  A. 深度路由（纯规则，秒级）——快答要 L0，抉择要 L2/L3，纯倾诉不超 L1
  B. 后处理（纯函数，秒级）——禁用词、英文词、超长裁剪、语气检查
  C. 端到端（需要模型）——自动选档是否真的"快答短、难答深、无客服腔"

用法：python tests/stage4_style_test.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import Companion  # noqa: E402
from engine import emotion as emotion_engine  # noqa: E402
from engine import style as style_engine  # noqa: E402
from router.depth_router import route_depth  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp"
TEST_DB = TMP / "memory_s4.db"
TEST_INDEX = TMP / "embeddings_s4.json"
TEST_PROFILE = TMP / "profile_s4.json"
TEST_VALUES = TMP / "values_s4.yaml"
TEST_SCENARIOS = TMP / "scenarios_s4.jsonl"
TEST_PREFERENCES = TMP / "preferences_s4.jsonl"
SAMPLES = ROOT / "samples"

# ---- A. 深度路由：不调模型 ----
ROUTE_CASES = [
    ("今天吃什么？", "l0"),
    ("现在几点了？", "l0"),
    ("你叫什么名字？", "l0"),
    ("今天有点累。", "l1"),
    ("推荐一部电影吧。", "l0"),   # "推荐"属快答：给个片名就够，不用长篇
    ("我心里特别堵，什么都不想干。", "l1"),
    ("我该不该换工作？", "l2"),
    ("要不要换个城市生活，我一直在纠结。", "l2"),
    ("我该不该换工作，这事关系到长期规划，我已经想了好几个月了。", "l3"),
    ("帮我分析一下，长期来看这份工作值不值得留下，利弊我想不通。", "l3"),
]

# ---- B. 后处理 ----
POST_CASES = [
    ("您好，很高兴为您服务。", "banned"),
    ("综上所述，你应该早点休息。", "banned"),
    ("我今天 just 想躺着，maybe 晚点出去走走。", "english"),
    ("这是一个很长很长很长的回答。" * 12, "truncated"),
    ("首先，这件事要从三个方面来看。其次，最后还有一点。", "tone"),
]


def part_a() -> tuple[int, list[str]]:
    print("=" * 66)
    print("A. 深度路由（规则）")
    print("=" * 66)
    passed, lines = 0, []
    for text, want in ROUTE_CASES:
        got, reason = route_depth(text, emotion_engine.analyze(text))
        ok = got == want
        passed += ok
        print(f"{'✓' if ok else '✗'} [{got.upper()} 期望 {want.upper()}] {text[:34]}")
        print(f"      {reason}")
        if not ok:
            lines.append(f"路由不符：{text} → {got}（期望 {want}）")
    print(f"A. 路由正确 {passed}/{len(ROUTE_CASES)}\n")
    return passed, lines


def part_b() -> tuple[int, list[str]]:
    print("=" * 66)
    print("B. 后处理（纯函数）")
    print("=" * 66)
    style = style_engine.load_style()
    passed, lines = 0, []

    fixed, report = style_engine.postprocess("您好，很高兴为您服务。", "l1", style)
    ok = "您好" not in fixed and "很高兴为您服务" not in fixed
    passed += ok
    print(f"{'✓' if ok else '✗'} 禁用词（客服腔）→ {fixed!r}")
    if not ok:
        lines.append("客服腔禁用词未被清除")

    fixed, report = style_engine.postprocess("综上所述，你应该早点休息。", "l1", style)
    ok = "综上所述" not in fixed
    passed += ok
    print(f"{'✓' if ok else '✗'} 禁用词（综上所述）→ {fixed!r}")
    if not ok:
        lines.append("综上所述未被替换")

    fixed, report = style_engine.postprocess("我今天 just 想躺着，maybe 晚点出去走走。", "l1", style)
    ok = not any(ch.isascii() and ch.isalpha() for ch in fixed)
    passed += ok
    print(f"{'✓' if ok else '✗'} 英文词还原 → {fixed!r}（替换 {report['english']}）")
    if not ok:
        lines.append("英文词未还原干净")

    long_text = "这是一个很长很长很长的回答。" * 12
    fixed, report = style_engine.postprocess(long_text, "l0", style)
    limit = style_engine.length_limit("l0", style)
    ok = len(fixed) <= limit and fixed.endswith(("。", "？", "！"))
    passed += ok
    print(f"{'✓' if ok else '✗'} 长度裁剪 L0：{len(long_text)} 字 → {len(fixed)} 字（上限 {limit}）")
    if not ok:
        lines.append("L0 超长回复未被正确裁剪")

    problems = style_engine.inspect_tone("首先，其次，最后，总之这件事很复杂。", style)
    ok = any("客服腔" in item for item in problems)
    passed += ok
    print(f"{'✓' if ok else '✗'} 语气检查 → {problems}")
    if not ok:
        lines.append("语气检查没有识别出模板腔")

    print(f"B. 后处理通过 {passed}/5\n")
    return passed, lines


def part_c() -> tuple[int, list[str], dict]:
    print("=" * 66)
    print("C. 端到端（自动选档 + 风格）")
    print("=" * 66)
    TMP.mkdir(parents=True, exist_ok=True)
    for path in (TEST_DB, pathlib.Path(str(TEST_DB) + "-wal"),
                 pathlib.Path(str(TEST_DB) + "-shm"), TEST_INDEX, TEST_PROFILE,
                 TEST_VALUES, TEST_SCENARIOS, TEST_PREFERENCES):
        if path.exists():
            path.unlink()

    companion = Companion(use_memory=True, session="styletest",
                          memory_path=TEST_DB, index_path=TEST_INDEX,
                          profile_path=TEST_PROFILE, values_path=TEST_VALUES,
                          scenarios_path=TEST_SCENARIOS,
                          preferences_path=TEST_PREFERENCES)
    style = style_engine.load_style()

    cases = [
        ("简单", "今天吃什么？", ("l0",), 60),
        ("简单", "现在几点了？", ("l0",), 60),
        ("复杂", "我该不该换工作，这事关系到长期规划，我已经想了好几个月了。", ("l2", "l3"), 400),
        ("情绪", "我今天特别难受，什么都不想干。", ("l1",), 200),
    ]

    passed, lines = 0, []
    lengths: dict[str, list[int]] = {"简单": [], "复杂": []}
    for kind, text, want_levels, limit in cases:
        reply, _info = companion.say(text)
        level = companion.last_level
        ok_level = level in want_levels
        ok_len = len(reply) <= limit
        problems = style_engine.inspect_tone(reply, style)
        ok_tone = not any("客服腔" in item for item in problems)
        ok = ok_level and ok_len and ok_tone
        passed += ok
        if kind in lengths:
            lengths[kind].append(len(reply))
        print(f"{'✓' if ok else '✗'} [{kind}] {text[:30]}")
        print(f"     选档 {level.upper()}（期望 {'/'.join(l.upper() for l in want_levels)}）"
              f"｜{companion.last_level_reason}")
        print(f"     长度 {len(reply)} 字（上限 {limit}）｜语气问题 {problems or '无'}")
        print(f"     回复：{reply[:90]}")
        if not ok:
            lines.append(f"端到端不符：{text} → {level}/长度 {len(reply)}/语气 {problems}")
        sys.stdout.flush()

    avg_simple = sum(lengths["简单"]) / max(1, len(lengths["简单"]))
    avg_complex = sum(lengths["复杂"]) / max(1, len(lengths["复杂"]))
    contrast_ok = avg_simple < avg_complex
    passed += contrast_ok
    print(f"\n{'✓' if contrast_ok else '✗'} 长度对照：简单问题平均 {avg_simple:.0f} 字"
          f" < 复杂问题平均 {avg_complex:.0f} 字")
    if not contrast_ok:
        lines.append("简单问题没有比复杂问题更短")

    print(f"C. 端到端通过 {passed}/{len(cases)+1}")
    companion.memory.close()
    metrics = {"avg_simple": avg_simple, "avg_complex": avg_complex}
    return passed, lines, metrics


def main() -> int:
    started = time.time()
    a_pass, a_fail = part_a()
    b_pass, b_fail = part_b()
    c_pass, c_fail, metrics = part_c()

    total_pass = a_pass + b_pass + c_pass
    total = len(ROUTE_CASES) + 5 + 5
    failures = a_fail + b_fail + c_fail

    print("\n" + "=" * 66)
    print(f"总计 {total_pass}/{total}　耗时 {time.time()-started:.1f} 秒")
    for item in failures:
        print(f"  ✗ {item}")

    SAMPLES.mkdir(exist_ok=True)
    report = SAMPLES / f"{time.strftime('%Y%m%d-%H%M%S')}-阶段4-深度路由与风格测试.md"
    lines = ["# 阶段 4 深度路由与风格引擎测试", "",
             f"- 路由正确 {a_pass}/{len(ROUTE_CASES)}",
             f"- 后处理通过 {b_pass}/5",
             f"- 端到端通过 {c_pass}/5",
             f"- 长度对照：简单问题平均 {metrics['avg_simple']:.0f} 字，"
             f"复杂问题平均 {metrics['avg_complex']:.0f} 字", ""]
    if failures:
        lines += ["## 未通过", ""] + [f"- {item}" for item in failures] + [""]
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已保存：{report}")

    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
