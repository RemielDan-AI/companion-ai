"""风格引擎：把画像里的"怎么说话"注入提示，并对生成结果做后处理。

两块职责：
1. 注入：按档位拼一小段风格要求（保持很短，模型对系统提示长度敏感）
2. 后处理：禁用词替换、英文词还原、长度裁剪、语气检查

后处理是最后一道闸——提示层管不住的毛病（英文词、客服腔套话、超长回复）在这里兜住。
"""
from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
STYLE_PATH = ROOT / "config" / "style.yaml"

# 语气检查用的客服腔特征（不只是禁用词，还包括结构性的套路）
SERVICE_TELLS = [
    "您好", "亲，", "很高兴为您服务", "有什么可以帮您", "希望这些信息",
    "综上所述", "首先", "其次", "总之", "感谢您的",
]
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def load_style(path: pathlib.Path | str = STYLE_PATH) -> dict:
    path = pathlib.Path(path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def length_limit(level: str, style: dict | None = None) -> int:
    style = style if style is not None else load_style()
    limits = style.get("length_limit") or {}
    return int(limits.get(level, 200))


def render_style_block(level: str, style: dict | None = None) -> str:
    """拼一段很短的风格要求，插进系统提示。"""
    style = style if style is not None else load_style()
    limit = length_limit(level, style)
    parts = [f"风格：这次回复控制在 {limit} 字以内"]
    if style.get("formality") == "低":
        parts.append("说话随意、不端着")
    if style.get("humor") == "少量":
        parts.append("偶尔可以带点玩笑，别硬抖机灵")
    if style.get("emoji") in ("不用", "少用"):
        parts.append("不用表情符号")
    return "，".join(parts) + "。"


def fix_english(text: str, style: dict | None = None) -> tuple[str, list[str]]:
    """把模型蹦出来的英文词换回中文。返回 (新文本, 被替换的词)。"""
    style = style if style is not None else load_style()
    fixes = style.get("english_fixes") or {}
    hits: list[str] = []
    result = text
    for word, replacement in fixes.items():
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", re.IGNORECASE)
        if pattern.search(result):
            hits.append(word)
            result = pattern.sub(replacement, result)
    # 替换后可能出现"就 静静"这类多余空格，清一下
    result = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", result)
    return result, hits


def replace_banned(text: str, style: dict | None = None) -> tuple[str, list[str]]:
    """禁用词替换/删除，并收拾留下的标点。"""
    style = style if style is not None else load_style()
    banned = style.get("banned_phrases") or {}
    hits: list[str] = []
    result = text
    for phrase, replacement in banned.items():
        if phrase in result:
            hits.append(phrase)
            result = result.replace(phrase, replacement)
    result = re.sub(r"[，、]{2,}", "，", result)
    result = re.sub(r"^[，、。；]+", "", result)
    result = re.sub(r"。{2,}", "。", result)
    result = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", result)
    return result.strip(), hits


_ECHO_QUOTE = re.compile(r"「([^」]{4,})」|“([^”]{4,})”|\"([^\"]{4,})\"")


def _echo_norm(text: str) -> str:
    """复述检测用的归一化：去掉标点与空白，避免标点差异漏判。"""
    return re.sub(r"[\s。，、；：！？…·\"'“”‘’「」（）()【】\[\]]", "", text or "")


def strip_echo(text: str, user_text: str = "") -> tuple[str, bool]:
    """去掉"复述对方原话"的毛病。

    实测四个模型在「你记一下」这类回合都会回「记下了——「<对方原话>」」，
    所以：引用块里如果是对方说过的话，就把引用整段拿掉；
    剩下的确认语保留（"记下了"），必要时补个句号。
    """
    if not text:
        return text, False
    user = _echo_norm(user_text)
    hit = False

    def repl(match: re.Match) -> str:
        nonlocal hit
        inner = (match.group(1) or match.group(2) or match.group(3) or "")
        norm = _echo_norm(inner)
        if len(norm) < 4:
            return match.group(0)
        # 引用内容与对方原话高度重合（包含或反向包含）就删掉
        if user and (norm in user or user in norm) and len(norm) >= max(4, len(user) * 0.6):
            hit = True
            return ""
        return match.group(0)

    cleaned = _ECHO_QUOTE.sub(repl, text)
    if hit:
        cleaned = re.sub(r"[—\-–]{1,2}\s*(?=[。！？]|$)", "", cleaned)
        cleaned = re.sub(r"[，、；]{2,}", "，", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" 　—，、；")
        if cleaned and cleaned[-1] not in "。！？…":
            cleaned += "。"
    return cleaned, hit


def strip_claims(text: str, marks: tuple[str, ...] = (),
                 fallback: str | None = None) -> tuple[str, bool]:
    """剪掉**断言式旧事指涉**的那一小句（没有记忆依据时的确定性兜底）。

    ## 为什么要有这个

    实测（`tests/analysis/fabrication_probe.py`）：上下文完全为空时，模型仍会说

        刚跟朋友吵了一架 → 「你**上次也**吵了。这次是因为什么？」
        我妈又催我相亲了 → 「你**上次也**提过这事。」

    基线 **6/24 = 25%**。`app.py` 里本来有"无来源断言守卫"，但只是**重生成一次**——
    重生成还编就放行了；补全 `CLAIM_MARKS` 之后实测仍是 **5/24 = 21%**。
    **光靠提示词和重试都治不住，所以要确定性剪掉。**

    ## 怎么剪

    按句号/问号/感叹号切句，**只丢掉含标记的那句**，其余保留。
    全丢光时返回一句安全兜底（"这个我记不太清了，你再跟我说说？"）——
    和 `clarify` 那套"不确定就问一句"的口径一致，不会显得冷淡。

    ⚠ 只在**没有记忆块**时调用（见 `app.py`）：有块时「以前」可能是正确答案。
    """
    # ⚠ 兜底句从 `config/replies.yaml` 现取（改文件即改话术）。
    #   调用方显式传 fallback 时以调用方为准（两段式补充要传 "" 表示"剪光就别补"）。
    if fallback is None:
        try:
            from engine import replies
            fallback = replies.get("no_memory", "这个我记不太清了，你再跟我说说？")
        except Exception:
            fallback = "这个我记不太清了，你再跟我说说？"

    src = str(text or "")
    if not src or not marks:
        return src, False
    hit = any(m in src for m in marks)
    if not hit:
        return src, False
    # 按句末标点切，保留分隔符
    parts = re.findall(r"[^。！？!?…]*[。！？!?…]?", src)
    kept = [p for p in parts if p and not any(m in p for m in marks)]
    out = "".join(kept).strip()
    if not out:
        # ⚠ allback 要能被调用方换掉（2026-10-20 实测）：
        #   主回复剪光时回一句安全话是好的；但**两段式的补充剪光时必须返回空**——
        #   否则会拼出「…你不是一个人扛的。这个我记不太清了，你再跟我说说？」
        #   这种**自相矛盾**的句子。补充剪光 = 这轮没补，直接丢。
        return fallback, True
    if out[-1] not in "。！？!?…":
        out += "。"
    return out, True


def trim_dangling_end(text: str) -> tuple[str, bool]:
    """被 max_tokens 截断时收个尾：切到最后一个完整句，避免半句话。"""
    text = (text or "").rstrip()
    if not text or text[-1] in "。！？…!?":
        return text, False
    cut = max(text.rfind(mark) for mark in ("。", "！", "？", "…"))
    if cut >= max(4, len(text) * 0.5):
        return text[:cut + 1], True
    return text + "…", True


def clamp_length(text: str, limit: int) -> tuple[str, bool]:
    """按句号/问号/感叹号在限度内收尾，尽量不切断句子。"""
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = text[:limit]
    for mark in ("。", "？", "！", "…"):
        cut = head.rfind(mark)
        if cut >= limit * 0.5:
            return head[:cut + 1], True
    # 找不到合适断点就退到最近的逗号，仍不行就硬截并补句号
    cut = head.rfind("，")
    if cut >= limit * 0.5:
        return head[:cut] + "。", True
    return head.rstrip("，、；") + "。", True


def is_latin_dominant(text: str) -> bool:
    """整段是不是以外文（拉丁字母）为主——英文回复不该被"英文词还原"改坏。"""
    if not text:
        return False
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return letters >= max(6, len(text) * 0.4)


def inspect_tone(text: str, style: dict | None = None,
                 latin: bool | None = None) -> list[str]:
    """语气检查：返回命中的问题清单（不做修改，供调用方决定）。"""
    style = style if style is not None else load_style()
    if latin is None:
        latin = is_latin_dominant(text)
    problems: list[str] = []
    for tell in SERVICE_TELLS:
        if tell in text:
            problems.append(f"客服腔：{tell}")
    if not latin:      # 英文/外文回复本来就该有字母，别误报成"残留英文"
        leftovers = [w for w in _LATIN_WORD.findall(text)
                     if w.lower() not in {k.lower() for k in (style.get("english_fixes") or {})}
                     and w.upper() not in {"AI", "OK"}]
        if leftovers:
            problems.append("残留英文：" + "、".join(leftovers[:3]))
    if text.count("？") + text.count("?") >= 3:
        problems.append("反问过多")
    if len(text) > 300 and text.count("\n") >= 4:
        problems.append("条目化过长")
    return problems


def postprocess(text: str, level: str, style: dict | None = None,
                user_text: str = "", finish_reason: str = "") -> tuple[str, dict]:
    """跑完整后处理，返回 (成品文本, 处理报告)。

    user_text     用于检测并去掉"复述对方原话"的毛病；
    finish_reason 为 "length" 说明被 max_tokens 截断，这里补个完整收尾。
    """
    style = style if style is not None else load_style()
    report: dict = {}
    fixed, echo_hit = strip_echo(text, user_text)
    report["echo"] = echo_hit
    latin = is_latin_dominant(fixed)
    if latin:
        # 外文回复不做英文词还原，否则 just→就 / maybe→可能 会把句子改坏
        fixed, english_hits = fixed, []
    else:
        fixed, english_hits = fix_english(fixed, style)
    fixed, banned_hits = replace_banned(fixed, style)
    limit = length_limit(level, style)
    fixed, truncated = clamp_length(fixed, limit)
    if finish_reason == "length" and not truncated:
        fixed, trimmed = trim_dangling_end(fixed)
        report["trimmed"] = trimmed
    report["english"] = english_hits
    report["banned"] = banned_hits
    report["truncated"] = truncated
    report["latin"] = latin
    report["problems"] = inspect_tone(fixed, style, latin=latin)
    return fixed, report
