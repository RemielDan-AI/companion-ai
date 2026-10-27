#!/usr/bin/env python
"""阶段 16 记忆导入测试（项目改进v1 改进 25）。

验收：支持从日记/笔记导入、自动提取分类、导入前预览、用户确认后写入、导入日志可查。
用假生成器（不依赖模型）验证提取与写入链路。

用法：python tests/stage16_import_test.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition import importer  # noqa: E402
from cognition.memory import Memory  # noqa: E402
from cognition.vectors import VectorIndex  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "import"


class FakeGenerator:
    """假生成器：按关键词给出固定的提取结果，不依赖模型。"""

    calls = 0

    def complete(self, system, user, max_tokens=400, temperature=0.2):
        FakeGenerator.calls += 1
        text = str(user)
        facts, prefs = [], []
        if "杭州" in text:
            facts.append("对方住在杭州")
        if "猫" in text:
            facts.append("对方养了一只猫叫团子")
        if "咖啡" in text:
            prefs.append("对方喜欢喝美式咖啡")
        return json.dumps({"summary": text[:20], "emotion": "平静", "importance": 3,
                           "facts": facts, "preferences": prefs, "relations": []},
                          ensure_ascii=False)


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    memory = Memory(TMP / "mem.db")
    index = VectorIndex(TMP / "index.json")
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 1. 读取来源：文件与目录 ----
    note = TMP / "日记.md"
    note.write_text("# 十月\n\n今天搬到杭州了，房子朝南。\n\n"
                    "养了一只猫叫团子，很黏人。\n\n晚上喝美式咖啡加班。\n",
                    encoding="utf-8")
    other = TMP / "笔记.txt"
    other.write_text("周末打算回老家看爸妈。", encoding="utf-8")
    jsonl = TMP / "备忘.jsonl"
    jsonl.write_text("\n".join(json.dumps({"text": f"第{i}条备忘，住在杭州"} ,
                                          ensure_ascii=False) for i in range(1, 4)),
                     encoding="utf-8")
    ignored = TMP / "图片.png"
    ignored.write_bytes(b"\x89PNG")

    docs = importer.load_documents(TMP)
    check("目录里能扫到支持的格式", len(docs) == 5, f"{len(docs)} 个文档")
    check("不支持的格式被忽略",
          all("png" not in doc["path"] for doc in docs), "")
    check("jsonl 按行拆成多条",
          sum(1 for doc in docs if doc["title"].startswith("备忘")) == 3, "")
    try:
        importer.list_sources(TMP / "不存在.txt")
        check("找不到文件时报错", False, "")
    except FileNotFoundError as exc:
        check("找不到文件时报错", "找不到" in str(exc), str(exc)[:30])

    chunks = importer.chunk_text(note.read_text(encoding="utf-8"))
    check("长文档按段落切块", len(chunks) >= 3, f"{len(chunks)} 块")
    long_text = "句子。" * 300
    check("超长段落会再按长度切",
          all(len(piece) <= importer.MAX_CHUNK for piece in importer.chunk_text(long_text)),
          str([len(p) for p in importer.chunk_text(long_text)][:3]))

    # ---- 2. 预览：只提取、不写库 ----
    before_count = memory.semantic_count("active")
    plan = importer.preview(note, FakeGenerator())
    check("预览出提取结果", plan["chunks"] >= 3 and plan["items"], f"{plan['chunks']} 段")
    facts_all = [f for item in plan["items"] for f in item["facts"]]
    prefs_all = [p for item in plan["items"] for p in item["preferences"]]
    check("事实与偏好被分开提取",
          any("住在杭州" in f for f in facts_all)
          and any("美式" in p for p in prefs_all), str(facts_all + prefs_all)[:80])
    check("预览不写库（用户还没确认）",
          memory.semantic_count("active") == before_count,
          f"{memory.semantic_count('active')} == {before_count}")

    # ---- 3. 确认后写入 ----
    report = importer.commit(plan["items"], memory, index, source_tag="日记.md")
    check("确认后写入了记忆",
          memory.semantic_count("active") > before_count, str(report))
    contents = [item["content"] for item in memory.all_semantic()]
    check("写进来的是结构化事实/偏好",
          any("杭州" in c for c in contents) and any("美式" in c for c in contents),
          str(contents)[:80])
    # 向量索引要联网调嵌入服务；服务没起也必须能导入（只是没有向量检索）
    check("嵌入服务没起也不影响导入（降级不报错）",
          report["written"] >= 3 and memory.semantic_count("active") >= 3,
          f"向量 {len(index)} 条，语义 {memory.semantic_count('active')} 条")

    # ---- 4. 重复导入不产生重复（沿用去重写入） ----
    count_after_first = memory.semantic_count("active")
    second = importer.commit(importer.preview(note, FakeGenerator())["items"], memory, index)
    check("重复导入不会写重", memory.semantic_count("active") == count_after_first,
          f"{memory.semantic_count('active')} == {count_after_first}")
    evidence = [item["evidence_count"] for item in memory.all_semantic()
                if "杭州" in item["content"]]
    check("重复导入把证据数加上去", evidence and max(evidence) >= 2, str(evidence))

    # ---- 5. 导入日志可查 ----
    logs = importer.import_log(memory)
    check("导入日志能查", len(logs) >= 2 and all(item["action"] == "import" for item in logs),
          f"{len(logs)} 条")
    check("日志里写了新增了多少条",
          "新增事实" in str(logs[-1]["detail"]), str(logs[-1]["detail"])[:40])

    # ---- 6. 空内容与坏文件不炸 ----
    empty = TMP / "空.txt"
    empty.write_text("   \n\n  ", encoding="utf-8")
    check("空文件不算文档", not importer.load_documents(empty), "")
    broken = TMP / "坏.jsonl"
    broken.write_text("这不是 json\n{\"text\": \"这行是好的，住在杭州\"}\n", encoding="utf-8")
    check("jsonl 里的坏行被跳过", len(importer.load_documents(broken)) == 1, "")

    memory.close()
    shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"\n通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
