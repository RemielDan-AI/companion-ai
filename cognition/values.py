"""价值观养成：从对话里提取、验证、写入价值观，并用它约束推理。

流程：
  信号检测（规则，便宜）→ 候选提取（模型，只在有信号时才调）
  → 置信度累积（重复升权、跨情境再升权）→ 冲突检测与降权
  → 自然确认（置信度不够或有冲突时，下一轮自然问一句）
  → 注入推理约束（active 的条目，按优先级取前几条）

数据文件：
  config/values.yaml        格式与规则
  values/values.yaml        价值观库（养成写入）
  values/scenarios.jsonl    情境化训练数据（阶段 6 用）
  values/preferences.jsonl  偏好对数据（阶段 6 用）
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "config" / "values.yaml"
STORE_PATH = ROOT / "values" / "values.yaml"
SCENARIOS_PATH = ROOT / "values" / "scenarios.jsonl"
PREFERENCES_PATH = ROOT / "values" / "preferences.jsonl"

# ---------------- 信号检测（规则，不调模型） ----------------
SIGNAL_PATTERNS: dict[str, list[str]] = {
    "自我描述": ["我是", "我这人", "我这个人", "我从来不", "我特别", "我最在意",
                 "我受不了", "我讨厌", "对我来说", "我一直觉得", "我的原则"],
    "选择模式": ["我选择", "我决定", "我还是", "我最后", "我宁可", "我宁愿",
                 "我选了", "我宁愿不", "我放弃了"],
    "回避": ["不想说", "算了", "无所谓", "随便", "没什么", "不聊这个", "别提",
             "不想提", "换个话题"],
    "对他人评价": ["我觉得他", "我觉得她", "他们", "这种人", "那种人",
                   "我讨厌那种", "我不喜欢那种", "我就不喜欢"],
    "纠正": ["不是这样", "我说的是", "其实我", "我说错了", "更正", "应该是",
             "你没懂", "不是那个意思"],
}


def detect_signals(text: str, emotion_info: dict | None = None,
                   feedback: dict | None = None) -> list[dict]:
    """返回 [{type, quote, weight}]。规则命中，不说话就不产生信号。"""
    text = text or ""
    signals: list[dict] = []
    for kind, markers in SIGNAL_PATTERNS.items():
        for marker in markers:
            if marker in text:
                signals.append({"type": kind, "quote": marker, "weight": 2})
                break
    info = emotion_info or {}
    if info.get("intensity", 0) >= 4:
        signals.append({"type": "情绪强度", "quote": info.get("emotion", ""), "weight": 3})
    if feedback:
        rating = feedback.get("rating")
        if rating in ("up", "down"):
            signals.append({"type": "反馈", "quote": feedback.get("note", ""), "weight": 1})
        if rating == "correction":
            signals.append({"type": "纠正", "quote": feedback.get("note", ""), "weight": 3})
    return signals


# 有这些信号才值得花一次模型调用去提候选；单纯问事/闲聊不触发
PROPOSE_SIGNALS = {"自我描述", "选择模式", "纠正", "对他人评价", "情绪强度"}


def should_propose(signals: list[dict], text: str) -> bool:
    if len(text or "") < 6:
        return False
    types = {signal["type"] for signal in signals}
    return bool(types & PROPOSE_SIGNALS)


# ---------------- 库读写 ----------------
def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def normalize(text: str) -> str:
    return " ".join(str(text or "").split()).strip("。，,；; ")


def load_rules(path: pathlib.Path | str = RULES_PATH) -> dict:
    path = pathlib.Path(path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_store(path: pathlib.Path | str = STORE_PATH) -> dict:
    path = pathlib.Path(path)
    if not path.exists():
        return {"updated_at": "", "values": [], "pending": [], "awaiting": None}
    with open(path, "r", encoding="utf-8") as fh:
        store = yaml.safe_load(fh) or {}
    store.setdefault("values", [])
    store.setdefault("pending", [])
    store.setdefault("awaiting", None)
    return store


def save_store(store: dict, path: pathlib.Path | str = STORE_PATH) -> None:
    path = pathlib.Path(path)
    store["updated_at"] = now()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(store, fh, allow_unicode=True, sort_keys=False, width=100)


# ---------------- 相似度与合并 ----------------
def _bigrams(text: str) -> set[str]:
    chars = re.findall(r"[\u4e00-\u9fff]", text or "")
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def similarity(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 1.0 if normalize(a) == normalize(b) else 0.0
    return len(ga & gb) / len(ga | gb)


def find_similar(store: dict, content: str, threshold: float = 0.42) -> dict | None:
    best, best_score = None, 0.0
    for entry in store.get("values", []):
        score = similarity(entry.get("content", ""), content)
        if score > best_score:
            best, best_score = entry, score
    return best if best_score >= threshold else None


def find_by_content(store: dict, content: str) -> dict | None:
    target = normalize(content)
    for entry in store.get("values", []):
        if normalize(entry.get("content", "")) == target:
            return entry
    return find_similar(store, content, threshold=0.45)


def next_id(store: dict) -> str:
    numbers = [int(m.group(1)) for m in
               (re.match(r"V(\d+)", str(v.get("id", ""))) for v in store.get("values", [])) if m]
    return f"V{(max(numbers) + 1) if numbers else 1:03d}"


def queue_confirmation(store: dict, entry: dict, reason: str, note: str = "") -> None:
    pending = store.setdefault("pending", [])
    existing = next((item for item in pending if item.get("value_id") == entry["id"]), None)
    if existing:
        # 已经在等确认了：冲突是更强的信号，把原来的低置信度确认升级掉
        if reason == "conflict" and existing.get("reason") != "conflict":
            existing["reason"] = "conflict"
            existing["note"] = note
            existing["asked"] = False
        return
    if len(pending) >= 3:  # 别攒太多，不然每轮都在问
        return
    pending.append({
        "value_id": entry["id"], "content": entry.get("content", ""),
        "reason": reason, "note": note, "asked": False, "created_at": now(),
    })


# ---------------- 候选提取（模型） ----------------
EXTRACT_SYSTEM = "你是价值观整理器。只输出一个 JSON 对象，不要解释，不要代码块。"

EXTRACT_TEMPLATE = """对方说：{user}

未央答：{reply}

本轮检测到的信号：{signals}

已有的、已验证的原则（只看是否冲突，没有就忽略）：
{existing}

判断这一轮是否暴露了对方的稳定价值观——他在意什么、怎么权衡、什么不肯让步。
严格输出 JSON：
{{"values": [{{"content": "用对方的语气陈述，不超过20字",
  "type": "决策原则/关系边界/沟通方式/生活态度/价值排序",
  "priority": 1到5的整数,
  "context": "触发情境，如 换工作"}}],
 "conflicts": [{{"existing": "与上面某条已有原则冲突的原文", "reason": "为什么冲突"}}]}}

规则：
- 只写对方明确表达过、且看起来是稳定倾向的；一次性的心情、具体安排、普通偏好不要写。
- 没有就输出空数组，不要硬凑。
- conflicts 只在确实和已有原则矛盾时才写。"""


def propose_candidates(generator, user_text: str, reply: str, signals: list[dict],
                       store: dict) -> dict:
    active = [v for v in store.get("values", []) if v.get("status") in ("active", "candidate")]
    existing = "\n".join(f"- {v.get('content')}" for v in active[:8]) or "（暂无）"
    signal_text = "、".join(f"{s['type']}({s['quote']})" for s in signals) or "（无）"
    prompt = EXTRACT_TEMPLATE.format(user=user_text, reply=reply,
                                     signals=signal_text, existing=existing)
    raw = ""
    try:
        raw = generator.complete(EXTRACT_SYSTEM, prompt, max_tokens=400, temperature=0.2)
    except Exception:
        return {"values": [], "conflicts": []}

    from cognition.extract import parse_json  # 复用同一套容错解析
    data = parse_json(raw) or {}
    values = []
    for item in data.get("values") or []:
        if isinstance(item, dict) and item.get("content"):
            try:
                priority = int(float(item.get("priority", 3)))
            except (TypeError, ValueError):
                priority = 3
            values.append({
                "content": normalize(item["content"])[:24],
                "type": str(item.get("type") or "生活态度").strip(),
                "priority": max(1, min(5, priority)),
                "context": normalize(item.get("context", ""))[:12],
            })
    conflicts = [c for c in (data.get("conflicts") or []) if isinstance(c, dict)]
    return {"values": values, "conflicts": conflicts}


# ---------------- 合并、升权、冲突 ----------------
def consolidate(store: dict, proposal: dict, signals: list[dict], turn: int,
                user_text: str, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    r = rules.get("rules", rules)  # 兼容直接传 rules 段
    events = {"new": [], "reinforced": [], "activated": [], "conflicts": []}
    evidence_base = {"turn": turn, "user_text": (user_text or "")[:60],
                     "signals": [s["type"] for s in signals]}

    for cand in proposal.get("values", []):
        content = cand["content"]
        if not content:
            continue
        match = find_similar(store, content)
        if match:
            match["evidence_count"] = int(match.get("evidence_count", 1)) + 1
            match["confidence"] = min(0.95, float(match.get("confidence", 0.3))
                                      + float(r.get("repeat_step", 0.08)))
            context = cand.get("context") or ""
            if context and context not in (match.get("contexts") or []):
                match.setdefault("contexts", []).append(context)
                match["confidence"] = min(0.95, match["confidence"]
                                          + float(r.get("cross_context_step", 0.05)))
                events["reinforced"].append(f"{match['id']} 跨情境新增「{context}」")
            else:
                events["reinforced"].append(f"{match['id']} 再次出现")
            match.setdefault("evidence", []).append(evidence_base)
            match["priority"] = max(int(match.get("priority", 3)), int(cand.get("priority", 3)))
            match["updated_at"] = now()
            if (match.get("status") == "candidate"
                    and match["confidence"] >= float(r.get("activate_threshold", 0.6))):
                match["status"] = "active"
                events["activated"].append(f"{match['id']} {match['content']}")
        else:
            entry = {
                "id": next_id(store), "content": content,
                "type": cand.get("type", "生活态度"),
                "priority": int(cand.get("priority", 3)),
                "confidence": float(r.get("new_confidence", 0.3)),
                "evidence_count": 1,
                "contexts": [cand["context"]] if cand.get("context") else [],
                "status": "candidate",
                "evidence": [evidence_base],
                "created_at": now(), "updated_at": now(),
            }
            store.setdefault("values", []).append(entry)
            events["new"].append(f"{entry['id']} {entry['content']}")
            if entry["confidence"] < float(r.get("activate_threshold", 0.6)):
                queue_confirmation(store, entry, reason="low_confidence")

    for conflict in proposal.get("conflicts", []):
        old = find_by_content(store, str(conflict.get("existing", "")))
        if not old:
            continue
        old["confidence"] = max(0.0, float(old.get("confidence", 0.3))
                                - float(r.get("conflict_penalty", 0.15)))
        old["updated_at"] = now()
        if old["confidence"] < float(r.get("archive_below", 0.12)):
            old["status"] = "archived"
        queue_confirmation(store, old, reason="conflict",
                           note=str(conflict.get("reason", ""))[:40])
        events["conflicts"].append(f"{old['id']} 被挑战，降到 {old['confidence']:.2f}")

    store["updated_at"] = now()
    return events


# ---------------- 自然确认 ----------------
def confirmation_hint(store: dict, rules: dict | None = None) -> str:
    """取出一个待确认项，标记为"已问"，返回可注入的提示。"""
    rules = rules or load_rules()
    pending = store.get("pending") or []
    unanswered = [item for item in pending if not item.get("asked")]
    if len(pending) >= 3 and not unanswered:
        store["pending"] = []
        unanswered = []
    if not unanswered:
        return ""
    item = unanswered[0]
    item["asked"] = True
    store["awaiting"] = {"value_id": item["value_id"], "content": item["content"],
                         "reason": item["reason"], "asked_at": now()}
    if item.get("reason") == "conflict":
        return (f"（对方这次的说法跟你原来记得的「{item['content']}」有点冲突。"
                f"如果话题合适，自然地问一句确认，别直接下结论，也别把这话说成什么大道理。）")
    return (f"（你还不确定对方是不是「{item['content']}」。"
            f"如果这轮话题合适，自然地问一句确认；不合适就别硬问，也别把这话说成什么大道理。）")


def resolve_confirmation(store: dict, user_text: str,
                         rules: dict | None = None) -> dict | None:
    """对方回答上一轮的自然确认。返回处理结果或 None。"""
    rules = rules or load_rules()
    awaiting = store.get("awaiting")
    if not awaiting:
        return None
    confirm = rules.get("confirm", {})
    text = user_text or ""
    agree = any(word in text for word in confirm.get("agree_words", []))
    disagree = any(word in text for word in confirm.get("disagree_words", []))
    if not agree and not disagree:
        return None

    entry = next((v for v in store.get("values", [])
                  if v.get("id") == awaiting.get("value_id")), None)
    store["awaiting"] = None
    store["pending"] = [item for item in (store.get("pending") or [])
                        if item.get("value_id") != awaiting.get("value_id")]
    if entry is None:
        return None

    if agree:
        entry["confidence"] = min(0.95, float(entry.get("confidence", 0.3))
                                  + float(confirm.get("agree_step", 0.18)))
        entry["evidence_count"] = int(entry.get("evidence_count", 1)) + 1
        entry.setdefault("evidence", []).append(
            {"turn": None, "user_text": text[:60], "signals": ["自然确认-认可"]})
        if entry["confidence"] >= float((rules.get("rules") or rules).get("activate_threshold", 0.6)):
            entry["status"] = "active"
        result = ("confirmed", entry["id"], entry["confidence"])
    else:
        entry["confidence"] = max(0.0, float(entry.get("confidence", 0.3))
                                  - float(confirm.get("disagree_step", 0.12)))
        if entry["confidence"] < float((rules.get("rules") or rules).get("archive_below", 0.12)):
            entry["status"] = "archived"
        result = ("rejected", entry["id"], entry["confidence"])
    entry["updated_at"] = now()
    return {"action": result[0], "value_id": result[1], "confidence": round(result[2], 2)}


# ---------------- 注入推理约束 ----------------
def values_block(store: dict, max_items: int | None = None,
                 rules: dict | None = None) -> str:
    rules = rules or load_rules()
    limit = int(max_items or (rules.get("rules", rules).get("max_inject", 4)))
    active = [v for v in store.get("values", []) if v.get("status") == "active"]
    if not active:
        return ""
    active.sort(key=lambda v: (-int(v.get("priority", 3)), -float(v.get("confidence", 0))))
    lines = "；".join(f"{v['content']}" for v in active[:limit])
    return (f"（他在意的原则，按重要性：{lines}。"
            "判断和给建议时按这个来，但别把这些话直接说出来。）")


# ---------------- 训练资产 ----------------
def _append_jsonl(path: pathlib.Path | str, payload: dict) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def record_scenario(context: str, user_text: str, reply: str, emotion: str = "",
                    tags: list[str] | None = None,
                    path: pathlib.Path | str = SCENARIOS_PATH) -> None:
    """情境化训练数据（阶段 6 用）。"""
    _append_jsonl(path, {"ts": now(), "context": context, "user": user_text,
                         "reply": reply, "emotion": emotion, "tags": tags or []})


def record_preference(prompt: str, chosen: str, rejected: str, source: str,
                      path: pathlib.Path | str = PREFERENCES_PATH) -> None:
    """偏好对数据（阶段 6 用）：chosen 是更好的那个回答。"""
    _append_jsonl(path, {"ts": now(), "prompt": prompt, "chosen": chosen,
                         "rejected": rejected, "source": source})


# ---------------- 展示 ----------------
def table(store: dict) -> str:
    values = store.get("values", [])
    if not values:
        return "价值观库还是空的（多聊聊你在意的事，它会慢慢长出来）"
    order = {"active": 0, "candidate": 1, "rejected": 2, "archived": 3}
    values = sorted(values, key=lambda v: (order.get(v.get("status"), 9),
                                           -int(v.get("priority", 3)),
                                           -float(v.get("confidence", 0))))
    lines = []
    for item in values:
        mark = {"active": "✅", "candidate": "·", "archived": "×"}.get(item.get("status"), "?")
        contexts = "/".join(item.get("contexts") or []) or "-"
        lines.append(f"{mark} {item.get('id'):<5}P{item.get('priority', 3)} "
                     f"置信 {item.get('confidence', 0):.2f} 证据 {item.get('evidence_count', 1)} "
                     f"〔{contexts}〕{item.get('content')}")
    pending = store.get("pending") or []
    if pending:
        lines.append(f"待确认 {len(pending)} 条：" +
                     "；".join(item.get("content", "") for item in pending))
    return "\n".join(lines)


def stats(store: dict) -> dict:
    values = store.get("values", [])
    return {
        "total": len(values),
        "active": sum(1 for v in values if v.get("status") == "active"),
        "candidate": sum(1 for v in values if v.get("status") == "candidate"),
        "archived": sum(1 for v in values if v.get("status") == "archived"),
        "pending": len(store.get("pending") or []),
    }
