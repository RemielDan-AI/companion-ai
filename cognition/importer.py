"""记忆导入（项目改进v1 改进 25）：把日记/笔记里的信息提取成结构化记忆。

流程刻意分成两步，**预览和写入分开**：
    preview(paths, generator)  → 只提取、不写库，给你看清楚要写什么
    commit(items, memory, ...) → 你确认之后才真正写入（并记导入日志）

支持 .txt / .md / .jsonl；每篇按空行/段落切块，逐块交给模型提取
（复用 cognition/extract.py 的提取逻辑：事实 / 偏好 / 关系 / 情绪 / 摘要）。
"""
from __future__ import annotations

import json
import pathlib

from cognition.extract import extract_turn
from cognition.memory import Memory

MAX_CHUNK = 600
SUPPORTED = (".txt", ".md", ".markdown", ".jsonl", ".json", ".log")


def list_sources(path) -> list[pathlib.Path]:
    """接受单个文件或目录，返回要导入的文件清单。"""
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到要导入的路径：{path}")
    if path.is_file():
        return [path]
    files = [p for p in sorted(path.rglob("*")) if p.is_file()
             and p.suffix.lower() in SUPPORTED]
    return files


def load_documents(path) -> list[dict]:
    """把文件读成 [{title, text}]；jsonl 按行当独立条目。"""
    docs: list[dict] = []
    for file in list_sources(path):
        try:
            raw = file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            docs.append({"title": file.name, "text": "", "error": str(exc),
                         "path": str(file)})
            continue
        if file.suffix.lower() == ".jsonl":
            for index, line in enumerate(raw.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                text = data.get("text") or data.get("content") or ""
                if text:
                    docs.append({"title": f"{file.name}#{index}", "text": str(text),
                                 "path": str(file)})
        else:
            docs.append({"title": file.name, "text": raw, "path": str(file)})
    return [doc for doc in docs if str(doc.get("text", "")).strip()]


def chunk_text(text: str, max_chars: int = MAX_CHUNK) -> list[str]:
    """按空行/换行切块，再按长度兜底切分。"""
    parts: list[str] = []
    for block in str(text).split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if len(block) <= max_chars:
            parts.append(block)
            continue
        buffer = ""
        for line in block.splitlines():
            if len(buffer) + len(line) + 1 > max_chars and buffer:
                parts.append(buffer.strip())
                buffer = line
            else:
                buffer = f"{buffer}\n{line}" if buffer else line
        if buffer.strip():
            parts.append(buffer.strip())
    # 兜底：单行本身超过上限时硬切，避免一坨太长
    out: list[str] = []
    for piece in parts:
        while len(piece) > max_chars:
            out.append(piece[:max_chars])
            piece = piece[max_chars:]
        if piece.strip():
            out.append(piece.strip())
    return out


def preview(path, generator, max_chunks: int = 40, max_chars: int = MAX_CHUNK) -> dict:
    """只看不写：把每块提取成结构化记忆，返回给用户确认。"""
    documents = load_documents(path)
    items: list[dict] = []
    errors: list[str] = []
    for doc in documents:
        for index, piece in enumerate(chunk_text(doc["text"], max_chars), 1):
            if len(items) >= max_chunks:
                break
            try:
                info = extract_turn(generator, piece, "")
            except Exception as exc:
                errors.append(f"{doc['title']}#{index}: {exc}")
                continue
            items.append({
                "source": doc["title"], "index": index, "text": piece,
                "summary": info.get("summary", ""),
                "emotion": info.get("emotion", ""),
                "importance": info.get("importance", 3),
                "facts": info.get("facts") or [],
                "preferences": info.get("preferences") or [],
                "relations": info.get("relations") or [],
            })
    return {"documents": len(documents), "chunks": len(items), "items": items,
            "errors": errors}


def commit(items: list[dict], memory: Memory, index=None, source_tag: str = "导入") -> dict:
    """确认后写入：语义记忆落库（去重靠 upsert），可选写向量索引，并记导入日志。"""
    written = 0
    added_facts = added_prefs = added_rel = 0
    vector_items: list[tuple[str, str]] = []
    for item in items:
        for kind, key in (("fact", "facts"), ("preference", "preferences"),
                          ("relation", "relations")):
            for content in item.get(key) or []:
                try:
                    semantic_id, created = memory.upsert_semantic(
                        kind, content, confidence=0.5, emotion=item.get("emotion") or "")
                except Exception:
                    continue
                written += 1
                if created:
                    if kind == "fact":
                        added_facts += 1
                    elif kind == "preference":
                        added_prefs += 1
                    else:
                        added_rel += 1
                    vector_items.append((f"s{semantic_id}", content))
    if index is not None and vector_items:
        try:
            index.add_texts(vector_items)
        except Exception:
            pass
    detail = (f"{source_tag}：新增事实 {added_facts}、偏好 {added_prefs}、关系 {added_rel}"
              f"（写入 {written} 条）")
    memory.log("import", "semantic", None, detail)
    return {"written": written, "facts": added_facts, "preferences": added_prefs,
            "relations": added_rel, "vectors": len(vector_items), "detail": detail}


def import_log(memory: Memory, limit: int = 20) -> list[dict]:
    """导入日志（改进 25：导入日志可查）。"""
    return memory.recent_logs(limit=limit, action="import")
