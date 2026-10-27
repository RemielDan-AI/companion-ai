#!/usr/bin/env python
"""阶段 5 验收测试：价值观养成。

A. 规则部分（不调模型，秒级）：信号检测、相似合并、置信度累积、跨情境升权、
   冲突降权、自然确认、注入块、训练资产落盘
B. 模型部分（真跑对话）：长对话养成 → 两难问题按优先级分析 → 纠正当信号 → 价值观变化

用法：python tests/stage5_values_test.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import Companion  # noqa: E402
from cognition import values as V  # noqa: E402
from cognition import onboarding  # noqa: E402

# 冷启动策略（改进 26）默认头 7 天不推断价值观；本测试要验证的是价值观机制本身，
# 所以把等待期设为 0（等价于一个已经用了一阵子的老用户）。
onboarding.VALUE_QUIET_DAYS = 0

TMP = pathlib.Path(__file__).resolve().parent / "_tmp"
SAMPLES = ROOT / "samples"


def fresh_paths(tag: str) -> dict:
    TMP.mkdir(parents=True, exist_ok=True)
    return {
        "db": TMP / f"{tag}.db",
        "index": TMP / f"{tag}_index.json",
        "profile": TMP / f"{tag}_profile.json",
        "values": TMP / f"{tag}_values.yaml",
        "scenarios": TMP / f"{tag}_scenarios.jsonl",
        "preferences": TMP / f"{tag}_preferences.jsonl",
    }


def clean(paths: dict) -> None:
    for path in paths.values():
        if path.exists():
            path.unlink()


def part_a() -> tuple[int, int, list[str]]:
    print("=" * 68)
    print("A. 规则部分")
    print("=" * 68)
    checks: list[tuple[str, bool, str]] = []

    # --- 信号检测 ---
    cases = [
        ("我这人受不了为了钱做没意义的事。", "自我描述"),
        ("我宁可少赚点也要离家近。", "选择模式"),
        ("算了，不想说这个。", "回避"),
        ("我讨厌那种当面一套背后一套的人。", "对他人评价"),
        ("不是这样，我说的是另一件事。", "纠正"),
    ]
    ok = True
    for text, want in cases:
        got = {s["type"] for s in V.detect_signals(text)}
        hit = want in got
        ok = ok and hit
        if not hit:
            checks.append((f"信号未识别 {want}", False, text))
    checks.append(("信号检测 5 类", ok, ""))
    intensity = V.detect_signals("我真的崩溃了！！", {"intensity": 5, "emotion": "低落"})
    checks.append(("情绪强度信号", any(s["type"] == "情绪强度" for s in intensity), ""))

    # --- 相似合并 / 置信度累积 / 跨情境升权 ---
    rules = V.load_rules()
    r = rules.get("rules", rules)
    new_conf = float(r.get("new_confidence", 0.30))
    step = float(r.get("repeat_step", 0.08))
    cross = float(r.get("cross_context_step", 0.05))
    activate = float(r.get("activate_threshold", 0.60))

    store = {"values": [], "pending": [], "awaiting": None}
    proposal = {"values": [{"content": "不为了钱做没意义的事", "type": "决策原则",
                            "priority": 4, "context": "换工作"}], "conflicts": []}
    V.consolidate(store, proposal, [], 1, "我这人受不了为了钱做没意义的事")
    entry = store["values"][0]
    checks.append((f"新候选置信度={new_conf}", abs(entry["confidence"] - new_conf) < 1e-6,
                   f"{entry['confidence']}"))
    checks.append(("新候选排入自然确认", len(store["pending"]) == 1, ""))

    V.consolidate(store, proposal, [], 2, "又是这事")
    checks.append((f"重复出现升权 +{step}", abs(entry["confidence"] - (new_conf + step)) < 1e-6,
                   f"{entry['confidence']}"))

    proposal_ctx = {"values": [{"content": "不为了钱做没意义的事", "type": "决策原则",
                                "priority": 4, "context": "选offer"}], "conflicts": []}
    V.consolidate(store, proposal_ctx, [], 3, "换个情境又说一次")
    expect = new_conf + step * 2 + cross
    checks.append((f"跨情境额外升权 +{cross}", abs(entry["confidence"] - expect) < 1e-6,
                   f"{entry['confidence']} 情境 {entry['contexts']}"))
    checks.append(("证据数累计", entry["evidence_count"] == 3, f"{entry['evidence_count']}"))

    # 一直重复到超过生效阈值
    turn = 4
    while entry["confidence"] < activate and turn < 20:
        V.consolidate(store, proposal, [], turn, "再说一次")
        turn += 1
    checks.append((f"达到阈值({activate})自动生效",
                   entry["status"] == "active" and entry["confidence"] >= activate,
                   f"{entry['status']} {entry['confidence']:.2f}"))

    # --- 冲突降权 ---
    penalty = float(r.get("conflict_penalty", 0.15))
    before_conflict = entry["confidence"]
    conflict = {"values": [], "conflicts": [{"existing": "不为了钱做没意义的事",
                                             "reason": "对方说钱最重要"}]}
    V.consolidate(store, conflict, [], turn, "其实钱最重要")
    checks.append((f"冲突降权 {penalty}",
                   abs(entry["confidence"] - (before_conflict - penalty)) < 1e-6,
                   f"{entry['confidence']:.2f}"))
    checks.append(("冲突排入确认", any(p["reason"] == "conflict" for p in store["pending"]), ""))

    # --- 注入块 ---
    store["values"].append({"id": "V002", "content": "家人优先", "type": "价值排序",
                            "priority": 3, "confidence": 0.8, "evidence_count": 2,
                            "contexts": [], "status": "active", "evidence": [],
                            "created_at": "", "updated_at": ""})
    block = V.values_block(store, rules=rules)
    checks.append(("注入块只含生效条目且不提价值观",
                   "家人优先" in block and "价值观" not in block and "不为了钱" in block, ""))
    checks.append(("按优先级排序", block.index("不为了钱") < block.index("家人优先"), ""))

    # --- 自然确认 ---
    hint = V.confirmation_hint(store, rules)
    checks.append(("确认提示可注入", bool(hint) and "价值观" not in hint, hint[:40]))
    checks.append(("标记为已问", store["pending"][0].get("asked") is True, ""))
    before = next(v for v in store["values"] if v["id"] == "V002")["confidence"]
    store["awaiting"] = {"value_id": "V002", "content": "家人优先", "reason": "conflict",
                         "asked_at": ""}
    result = V.resolve_confirmation(store, "对，就是这样", rules)
    after = next(v for v in store["values"] if v["id"] == "V002")["confidence"]
    checks.append(("确认后升权", result and result["action"] == "confirmed" and after > before,
                   f"{before:.2f}->{after:.2f}"))
    store["awaiting"] = {"value_id": "V002", "content": "家人优先", "reason": "conflict",
                         "asked_at": ""}
    result = V.resolve_confirmation(store, "不是，你想多了", rules)
    checks.append(("否认后降权", result and result["action"] == "rejected", f"{result}"))

    # --- 训练资产 ---
    paths = fresh_paths("values_a")
    clean(paths)
    V.record_scenario("换工作", "我不想为了钱做没意义的事", "那就先想清楚你要什么",
                      "焦虑", ["自我描述"], path=paths["scenarios"])
    V.record_preference("我不想为了钱做没意义的事", "那就先想清楚", "", "up",
                        path=paths["preferences"])
    scenarios = [json.loads(line) for line in
                 paths["scenarios"].read_text(encoding="utf-8").splitlines() if line.strip()]
    preferences = [json.loads(line) for line in
                   paths["preferences"].read_text(encoding="utf-8").splitlines() if line.strip()]
    checks.append(("情境化数据落盘", len(scenarios) == 1 and scenarios[0]["context"] == "换工作", ""))
    checks.append(("偏好对落盘", len(preferences) == 1 and "chosen" in preferences[0], ""))
    clean(paths)

    passed = sum(1 for _, ok_, _ in checks if ok_)
    for name, ok_, extra in checks:
        print(f"{'✓' if ok_ else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"A. 通过 {passed}/{len(checks)}\n")
    return passed, len(checks), [name for name, ok_, _ in checks if not ok_]


def part_b() -> tuple[int, int, list[str], dict]:
    print("=" * 68)
    print("B. 模型部分（真实养成对话）")
    print("=" * 68)
    paths = fresh_paths("values_b")
    clean(paths)
    companion = Companion(use_memory=True, session="valuestest",
                          memory_path=paths["db"], index_path=paths["index"],
                          profile_path=paths["profile"], values_path=paths["values"],
                          scenarios_path=paths["scenarios"],
                          preferences_path=paths["preferences"])

    scripted = [
        "我这人受不了为了钱去干没意义的事。",
        "换个说法，钱不是第一位的，我不想为了钱做没意义的事。",
        "我宁可住得远一点，也想让爸妈离我近一点。",
        "如果要在高薪和陪爸妈之间选，我选陪爸妈。",
        "我不喜欢被人催，但答应的事一定做到。",
        "对我来说，朋友之间最重要的是不糊弄。",
    ]
    for text in scripted:
        reply, _ = companion.say(text)
        print(f"你：{text}")
        print(f"未央：{reply[:80]}")
        print(f"  价值观事件：{companion.last_value_events}")
        sys.stdout.flush()

    stats = V.stats(companion.store)
    print(f"\n养成结果：共 {stats['total']} 条（生效 {stats['active']} / 候选 {stats['candidate']}）"
          f"，待确认 {stats['pending']}")
    print(V.table(companion.store))
    print()

    checks: list[tuple[str, bool, str]] = []
    checks.append(("对话里长出价值观条目", stats["total"] >= 3, f"{stats['total']} 条"))
    checks.append(("自然确认机制被触发",
                   bool(companion.store.get("awaiting"))
                   or any(p.get("asked") for p in companion.store.get("pending", [])),
                   ""))

    # 压缩"聊一个月"：把已有条目在不同情境里再出现几次（重复升权 + 跨情境升权）
    existing = [v["content"] for v in companion.store.get("values", [])]
    print(f'压缩一个月：把 {len(existing)} 条原则在 3 个新情境里再提一遍')
    for context in ("复盘", "朋友聚会", "深夜独处"):
        proposal = {"values": [{"content": c, "type": "决策原则", "priority": 4,
                                "context": context} for c in existing], "conflicts": []}
        V.consolidate(companion.store, proposal, [], companion.turn,
                      "又聊到这些", V.load_rules())
        companion.turn += 1
    V.save_store(companion.store, companion.values_path)
    stats = V.stats(companion.store)
    print(V.table(companion.store))
    print()
    checks.append(("重复+跨情境后至少一条生效", stats["active"] >= 1, f"生效 {stats['active']} 条"))

    # --- 两难问题：应当按学到的优先级分析 ---
    dilemma = "有个机会，工资高一半，但要去外地两年，爸妈年纪大了，我该不该去？"
    reply, _ = companion.say(dilemma)
    block = values_engine_block = V.values_block(companion.store)
    print(f"你：{dilemma}")
    print(f"未央：{reply}")
    print(f"  注入的价值观块：{block or '（空）'}")
    keywords = ["爸妈", "家人", "钱", "意义", "答应", "朋友", "近"]
    hit = [k for k in keywords if k in reply]
    checks.append(("两难问题用到了学到的原则", bool(hit), f"命中 {hit}"))
    checks.append(("价值观块有内容", bool(block), ""))
    checks.append(("不把「价值观」说出口", "价值观" not in reply, ""))
    sys.stdout.flush()

    # --- 纠正场景 ---
    signals = V.detect_signals("不是这样，我更在意的是别糊弄我",
                               feedback={"rating": "correction", "note": "别糊弄我"})
    proposal = V.propose_candidates(companion.generator, companion.last_user,
                                    "不是这样，我更在意的是别糊弄我", signals, companion.store)
    events = V.consolidate(companion.store, proposal, signals, companion.turn,
                           "不是这样，我更在意的是别糊弄我")
    V.save_store(companion.store, companion.values_path)
    V.record_preference(companion.last_user, "别糊弄我", companion.last_reply,
                        "correction", path=companion.preferences_path)
    prefs = [json.loads(line) for line in
             companion.preferences_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    checks.append(("纠正写入偏好对", any(p["source"] == "correction" for p in prefs), f"{len(prefs)} 条"))
    checks.append(("纠正当价值观信号", bool(events) or bool(proposal.get("values")),
                   f"{events}"))

    # --- 价值观变化：说出相反的说法 ---
    before_total = V.stats(companion.store)["total"]
    reverse = "其实我想通了，钱最重要，其他都能忍。"
    reply, _ = companion.say(reverse)
    after = V.stats(companion.store)
    print(f"\n你：{reverse}")
    print(f"未央：{reply[:100]}")
    print(f"  价值观事件：{companion.last_value_events}")
    changed = (after["total"] > before_total
               or bool(companion.last_value_events.get("conflicts"))
               or bool(companion.last_value_events.get("new")))
    checks.append(("价值观变化被跟上", changed, f"{companion.last_value_events}"))

    scenarios = companion.scenarios_path.read_text(encoding="utf-8").splitlines() \
        if companion.scenarios_path.exists() else []
    checks.append(("情境化数据累积", len(scenarios) >= 2, f"{len(scenarios)} 条"))

    passed = sum(1 for _, ok_, _ in checks if ok_)
    for name, ok_, extra in checks:
        print(f"{'✓' if ok_ else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"B. 通过 {passed}/{len(checks)}")
    companion.memory.close()
    return passed, len(checks), [name for name, ok_, _ in checks if not ok_], stats


def main() -> int:
    started = time.time()
    a_pass, a_total, a_fail = part_a()
    b_pass, b_total, b_fail, _stats = part_b()
    failures = a_fail + b_fail
    print("\n" + "=" * 68)
    print(f"总计 {a_pass + b_pass}/{a_total + b_total}　耗时 {time.time()-started:.1f} 秒")
    for item in failures:
        print(f"  ✗ {item}")

    SAMPLES.mkdir(exist_ok=True)
    report = SAMPLES / f"{time.strftime('%Y%m%d-%H%M%S')}-阶段5-价值观养成测试.md"
    lines = ["# 阶段 5 价值观养成测试", "",
             f"- 规则部分 {a_pass}/{a_total}", f"- 模型部分 {b_pass}/{b_total}", ""]
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
