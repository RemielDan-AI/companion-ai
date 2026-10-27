"""朗读前翻译：把回复文本翻成「朗读语言」，只用于语音，不改动聊天记录。

用在"文本语言 ≠ 朗读语言"的时候——比如未央写中文、你想听日语读出来。
翻译失败就退回原文，不让语音整条哑掉。
"""
from __future__ import annotations

LANG_NAMES = {"zh": "中文", "ja": "日语", "en": "英语"}

_SYSTEM = "你是翻译。只输出译文本身，不要解释、不要加引号、不要重复原文。"


def needs_translation(text_lang: str, voice_lang: str) -> bool:
    """文本语言与朗读语言不同才需要翻译。"""
    return bool(text_lang) and bool(voice_lang) and text_lang != voice_lang


def to_language(generator, text: str, target: str, max_tokens: int | None = None) -> str:
    """把 text 翻成 target 语言（zh/ja/en）。失败或结果为空时返回原文。"""
    text = (text or "").strip()
    if not text or target not in LANG_NAMES:
        return text
    name = LANG_NAMES[target]
    prompt = (f"把下面这段话翻译成{name}。保持口语和语气，"
              f"不要加任何解释，不要保留原文：\n\n{text}")
    limit = max_tokens or min(800, max(160, int(len(text) * 2.2)))
    try:
        out = generator.complete(_SYSTEM, prompt, max_tokens=limit, temperature=0.3)
    except Exception:
        return text
    out = (out or "").strip().strip('"').strip("“”「」").strip()
    return out or text
