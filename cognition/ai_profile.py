"""AI 自己的档案与动态性格（项目改进v1 模块二 改进 4）。

要点（对应改进项验收标准）：
- 名字/关系/称谓/语气可自选，**独立存储**在 config/ai_profile.yaml，
  与用户画像（cognition/profile.json）彻底分开——那份管「懂你」，这份管「它自己是谁」；
- 性格用几个 0–1 的维度表示（活泼度/话量/主动性/亲密感/玩笑度），随对话**小幅**调整；
- 调整有**频率限制**（至少隔 N 轮）与**步长上限**，避免性格频繁漂移；
- 每次调整都写进 history（含调整前后），**可回滚**，也可一键恢复默认；
- 维度只影响语气与话量，**不改底线**：底线与安全边界始终以 persona.yaml 为准。
"""
from __future__ import annotations

import datetime as dt
import pathlib

import yaml

PROFILE_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "ai_profile.yaml"

DIMENSIONS = ("liveliness", "talkativeness", "initiative", "closeness", "playfulness")
DIM_LABELS = {
    "liveliness": "活泼度", "talkativeness": "话量", "initiative": "主动性",
    "closeness": "亲密感", "playfulness": "玩笑度",
}

DEFAULTS: dict = {
    "identity": {"name": "未央", "relation": "灵魂知己",
                 "address_style": "不用称谓", "tone": "温柔体贴"},
    "traits": {"liveliness": 0.55, "talkativeness": 0.45, "initiative": 0.40,
               "closeness": 0.60, "playfulness": 0.35},
    "limits": {"max_delta_per_step": 0.06, "min_interval_turns": 6,
               "floor": 0.15, "ceiling": 0.90, "max_history": 60},
    "history": [],
}


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _merge(base: dict, loaded: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in (loaded or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def load(path: pathlib.Path | str | None = None) -> dict:
    path = pathlib.Path(path) if path else PROFILE_PATH
    if not path.exists():
        profile = _merge(DEFAULTS, {})
        save(profile, path)
        return profile
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        raw = {}
    return _merge(DEFAULTS, raw)


def save(profile: dict, path: pathlib.Path | str | None = None) -> None:
    path = pathlib.Path(path) if path else PROFILE_PATH
    profile["updated_at"] = now()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(profile, fh, allow_unicode=True, sort_keys=False, width=100)


# ---------------- 维度 → 提示词 ----------------
def hints(profile: dict) -> list[str]:
    """把性格维度翻译成简短的行为提示（越短越稳，小模型对长提示敏感）。"""
    traits = profile.get("traits") or {}
    out: list[str] = []

    def value(key: str, default: float = 0.5) -> float:
        try:
            return float(traits.get(key, default))
        except (TypeError, ValueError):
            return default

    talk = value("talkativeness")
    if talk <= 0.35:
        out.append("话少一点，一两句就够")
    elif talk >= 0.65:
        out.append("可以多说两句，但别啰嗦")

    ini = value("initiative")
    if ini <= 0.32:
        out.append("别追问，等对方开口")
    elif ini >= 0.62:
        out.append("可以主动关心一句")

    live = value("liveliness")
    if live <= 0.35:
        out.append("安静一些，别硬找话题")
    elif live >= 0.68:
        out.append("语气轻快一点")

    close = value("closeness")
    if close <= 0.35:
        out.append("保持一点分寸，别太亲昵")
    elif close >= 0.7:
        out.append("可以更亲近自然")

    play = value("playfulness")
    if play >= 0.6:
        out.append("可以轻松打趣")
    return out


def describe(profile: dict) -> str:
    ident = profile.get("identity") or {}
    traits = profile.get("traits") or {}
    lines = [
        f"名字：{ident.get('name', '未央')}　关系：{ident.get('relation', '伙伴')}"
        f"　称呼：{ident.get('address_style', '不用称谓')}　语气：{ident.get('tone', '温柔体贴')}",
        "性格维度：" + "　".join(
            f"{DIM_LABELS[key]} {float(traits.get(key, 0.5)):.2f}" for key in DIMENSIONS),
        "行为提示：" + ("；".join(hints(profile)) or "（默认）"),
    ]
    return "\n".join(lines)


def history_table(profile: dict, limit: int = 12) -> str:
    history = list(profile.get("history") or [])[-limit:]
    if not history:
        return "（还没有调整过）"
    lines = []
    for item in reversed(history):
        changes = "、".join(
            f"{DIM_LABELS.get(k, k)} {v[0]:.2f}→{v[1]:.2f}"
            for k, v in (item.get("changes") or {}).items())
        lines.append(f"· {item.get('time', '')} 第 {item.get('turn', '?')} 轮　"
                     f"{item.get('reason', '')}　{changes}")
    return "\n".join(lines)


# ---------------- 动态调整 ----------------
def signals_to_deltas(feedback: dict | None = None, emotion: dict | None = None,
                      user_text: str = "") -> tuple[dict, str]:
    """把这一轮的反馈/情绪/说话方式翻译成性格微调。返回 (增量, 原因)。"""
    deltas: dict[str, float] = {}
    reasons: list[str] = []
    feedback = feedback or {}
    rating = str(feedback.get("rating") or "")

    if rating in ("down", "correction"):
        deltas.update(talkativeness=-0.05, liveliness=-0.04, initiative=-0.05)
        reasons.append("用户点踩/纠错 → 收敛")
    elif rating == "up":
        deltas.update(closeness=0.03, initiative=0.02)
        reasons.append("用户点赞 → 亲近一点")

    emotion_label = str((emotion or {}).get("emotion") or "平静")
    intensity = int((emotion or {}).get("intensity") or 0)
    if emotion_label in ("难过", "疲惫", "焦虑", "生气") and intensity >= 3:
        deltas["talkativeness"] = deltas.get("talkativeness", 0.0) - 0.03
        deltas["initiative"] = deltas.get("initiative", 0.0) - 0.02
        reasons.append(f"对方{emotion_label} → 少说、少追问")
    elif emotion_label in ("开心", "期待") and intensity >= 3:
        deltas["liveliness"] = deltas.get("liveliness", 0.0) + 0.03
        deltas["playfulness"] = deltas.get("playfulness", 0.0) + 0.02
        reasons.append(f"对方{emotion_label} → 轻快一点")

    text = (user_text or "").strip()
    if text and len(text) <= 6:
        deltas["talkativeness"] = deltas.get("talkativeness", 0.0) - 0.02
        reasons.append("对方话很短 → 少说")
    elif len(text) >= 60:
        deltas["talkativeness"] = deltas.get("talkativeness", 0.0) + 0.02
        reasons.append("对方说得多 → 可以多接一点")

    return deltas, "；".join(reasons)


def adjust(profile: dict, turn: int, feedback: dict | None = None,
           emotion: dict | None = None, user_text: str = "",
           force: bool = False) -> dict:
    """按信号微调性格。返回 {changed, reason, changes}。

    频率限制：距离上次调整不足 min_interval_turns 轮就跳过（force=True 可绕过，
    用于用户手动指定性格）。
    """
    limits = profile.get("limits") or DEFAULTS["limits"]
    history = profile.setdefault("history", [])
    last_turn = history[-1].get("turn", -999) if history else -999
    interval = int(limits.get("min_interval_turns", 6))
    if not force and turn - int(last_turn) < interval:
        return {"changed": False, "reason": f"距上次调整不足 {interval} 轮", "changes": {}}

    deltas, reason = signals_to_deltas(feedback, emotion, user_text)
    if not deltas:
        return {"changed": False, "reason": "这一轮没有触发调整的信号", "changes": {}}

    step = float(limits.get("max_delta_per_step", 0.06))
    floor = float(limits.get("floor", 0.15))
    ceiling = float(limits.get("ceiling", 0.90))
    traits = profile.setdefault("traits", dict(DEFAULTS["traits"]))
    changes: dict[str, list[float]] = {}
    for key, delta in deltas.items():
        if key not in DIMENSIONS:
            continue
        before = float(traits.get(key, 0.5))
        delta = max(-step, min(step, float(delta)))          # 单次幅度封顶
        after = max(floor, min(ceiling, round(before + delta, 3)))
        if abs(after - before) < 1e-6:
            continue
        traits[key] = after
        changes[key] = [round(before, 3), after]
    if not changes:
        return {"changed": False, "reason": "已到边界，无需调整", "changes": {}}

    history.append({"time": now(), "turn": int(turn), "reason": reason,
                    "changes": changes, "feedback": (feedback or {}).get("rating", ""),
                    "emotion": (emotion or {}).get("emotion", "")})
    max_history = int(limits.get("max_history", 60))
    if len(history) > max_history:
        del history[: len(history) - max_history]
    return {"changed": True, "reason": reason, "changes": changes}


def set_trait(profile: dict, key: str, value: float, turn: int = 0) -> dict:
    """用户手动指定某个维度（绕过频率限制，但仍记录历史，可回滚）。"""
    if key not in DIMENSIONS:
        raise KeyError(f"未知维度：{key}（可选：{'、'.join(DIMENSIONS)}）")
    limits = profile.get("limits") or DEFAULTS["limits"]
    traits = profile.setdefault("traits", dict(DEFAULTS["traits"]))
    before = float(traits.get(key, 0.5))
    after = max(float(limits.get("floor", 0.15)),
                min(float(limits.get("ceiling", 0.90)), float(value)))
    traits[key] = round(after, 3)
    profile.setdefault("history", []).append({
        "time": now(), "turn": int(turn), "reason": "用户手动设置",
        "changes": {key: [round(before, 3), round(after, 3)]}, "manual": True})
    return profile["history"][-1]


def rollback(profile: dict, steps: int = 1) -> dict:
    """回滚最近 N 次调整（按历史里的调整前值还原）。"""
    history = profile.get("history") or []
    if not history:
        return {"ok": False, "reason": "没有可回滚的调整"}
    traits = profile.setdefault("traits", dict(DEFAULTS["traits"]))
    undone = 0
    for item in reversed(history[-int(steps):]):
        for key, pair in (item.get("changes") or {}).items():
            if key in DIMENSIONS and isinstance(pair, (list, tuple)) and len(pair) == 2:
                traits[key] = round(float(pair[0]), 3)
                undone += 1
    del history[len(history) - int(steps):]
    return {"ok": True, "undone": undone,
            "reason": f"已回滚 {steps} 次调整（还原 {undone} 个维度）"}


def reset(profile: dict, turn: int = 0) -> dict:
    """恢复默认性格（身份字段保持不变，历史留一条记录以便追溯）。"""
    before = {k: float((profile.get("traits") or {}).get(k, 0.5)) for k in DIMENSIONS}
    default_traits = dict(DEFAULTS["traits"])
    changes = {k: [before[k], default_traits[k]] for k in DIMENSIONS
               if abs(before[k] - default_traits[k]) > 1e-6}
    profile["traits"] = default_traits
    profile.setdefault("history", []).append({
        "time": now(), "turn": int(turn), "reason": "恢复默认性格",
        "changes": changes, "manual": True})
    return {"ok": True, "changes": changes}


def identity_of(profile: dict) -> dict:
    return dict(profile.get("identity") or DEFAULTS["identity"])
