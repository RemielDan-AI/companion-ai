# -*- coding: utf-8 -*-
"""校验文档里引用的文件路径是否真实存在（防接力时"照着找却找不到"）。

扫 结构.txt / 微调数据/README.md / test/README.md / 项目.txt，
抓出 `反引号包裹的路径` 与较明显的裸路径，逐个查是否存在。
"""
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# ⚠ 不写死绝对路径：按本文件位置往上游推（tests/analysis/ → 仓库根）
PROJ = pathlib.Path(__file__).resolve().parent.parent.parent

DOCS = [PROJ / "结构.txt", PROJ / "项目.txt",
        PROJ / "微调数据" / "README.md", PROJ / "test" / "README.md",
        PROJ / "微调数据" / "weiyang_fix_v1.3_怎么用.md"]

# 只校验"看起来是文件/目录"的路径：含 / 或 \ 或已知后缀，且不含通配符/占位词
SKIP_WORDS = ("（", "）", "<", ">", "*", "例", "如", "某文件", "路径", "别的目录",
              "你的模型关键字", "alias", "http", "%APPDATA%", "node_modules")
EXTS = (".py", ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".bat", ".log",
        ".gguf", ".db", ".bak")
# 纯扩展名碎片、以及"文件不在本仓库"的名字（模型在 <模型目录>）
IGNORE = {".bak", ".bat", ".jsonl", ".py", ".md", ".log", "d.py"}
MODEL_DIR = pathlib.Path(__import__("os").environ.get(
    "COMPANION_MODEL_DIRS", "").split(";")[0]
    or (pathlib.Path.home() / "ai" / "models"))

pat = re.compile(r"`([^`\n]{3,90})`")


# 已删除的文件在文档里会保留说明（划掉 + 「已删」），那些不是活链接，不该报错。
# 2026-10-15：`weiyang_fix_*` 三个导出文件被并入主训练集后删除，
# 文档里留了「~~文件名~~ 已删」的备注——校验器要能区分「说明它删了」和「指了个不存在的路径」。
#
# 2026-10-19：再加一类「**生成物**」——它不是被删了，而是**用脚本现算出来的**，
# 平时不存在是正常的。典型：`weiyang_train_mixed.jsonl`
# 由 `微调数据/主要数据/a.py` 把 `weiyang_train.jsonl` + `general_120.jsonl`
# 打乱混合生成，**每次微调前跑一次**。要求它常驻在磁盘上就是错的，
# 所以文档里标了「（生成物）」的引用不该报"找不到"。
DELETED_MARKS = ("已删", "已删除", "~~", "（删）", "计划删除",
                 "生成物", "生成文件", "现算", "脚本生成",
    # 2026-10-21 补：**运行时才创建**的路径（如 run_all 的 _tmp/runall/），
    # 文档里提到它是对的，但它不该被要求现在存在。
    "运行时生成", "运行时创建", "临时目录")


def candidates(text: str):
    for m in pat.finditer(text):
        s = m.group(1).strip()
        if any(w in s for w in SKIP_WORDS):
            continue
        if not (("/" in s or "\\" in s) or s.endswith(EXTS)):
            continue
        if not (s.endswith(EXTS) or s.endswith("/") or s.endswith("\\")):
            continue
        # 取该引用所在的那一行，若整行有"已删"标记则跳过
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start:line_end if line_end >= 0 else len(text)]
        if any(w in line for w in DELETED_MARKS):
            skipped_deleted.append(s)
            continue
        yield s


skipped_deleted: list[str] = []


bad = []
checked = 0
for doc in DOCS:
    if not doc.exists():
        continue
    text = doc.read_text(encoding="utf-8", errors="replace")
    for s in sorted(set(candidates(text))):
        # ⚠ 2026-10-27：去尾部分隔符之后可能变成空串
        #   （文档里出现裸 `/` 或 `\` 时，例如「转义换行/制表符」这种非路径写法），
        #   接下来 `.split()[0]` 会 **IndexError 直接崩掉整个检查**。
        #   → 先判空再 split。
        stripped = s.rstrip("/\\").strip()
        if not stripped:
            continue
        _parts = stripped.split()
        if not _parts:
            continue
        s2 = _parts[0]
        # 纯数字/括号片段（如 "l0 60" 被引号抓成 "(60)"）不是路径
        if len(s2) < 4 or s2.startswith("-") or s2 in IGNORE:
            continue
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", s2):
            continue
        if re.fullmatch(r"[()（）\d\.\s]+", s2):
            continue
        # 解析顺序：文档自身所在目录 → 项目根 → companion-ai → 模型目录（模型不在本仓库）
        bases = [doc.parent, PROJ, PROJ / "companion-ai", MODEL_DIR]
        if doc.parent.name in ("微调数据", "test"):
            bases += [doc.parent / "报告", doc.parent / "脚本", doc.parent / "logs"]
        # ⚠ **绝对路径要单独处理**（2026-10-20 修）：文档里会写
        #   `<模型目录>\...` 这种盘符开头的路径，拿它去 `base.glob()` 会抛
        #   `NotImplementedError: Non-relative patterns are unsupported`（Python 3.13），
        #   整个检查器直接崩。绝对路径直接判存在即可，不需要相对解析。
        if re.match(r"^[A-Za-z]:[\\/]", s2) or s2.startswith(("/", "\\")):
            if pathlib.Path(s2).exists():
                checked += 1
                continue
            bad.append((doc.name, s2))
            continue
        for base in bases:
            try:
                hit = (base / s2).exists() or bool(list(base.glob(s2)))
            except (NotImplementedError, ValueError, OSError):
                hit = False
            if hit:
                checked += 1
                break
        else:
            # 兜底①：文档里可能只写文件名（如表格列），全仓递归找同名文件
            if any(PROJ.rglob(pathlib.PurePath(s2).name)):
                checked += 1
                continue
            # 兜底②：模型文件在 <模型目录> 下，按名字递归找
            if s2.endswith(".gguf") and any(MODEL_DIR.rglob(pathlib.PurePath(s2).name)):
                checked += 1
                continue
            bad.append((doc.name, s2))

print(f"检查了 {checked} 条带路径的引用")
if skipped_deleted:
    print(f"（跳过 {len(skipped_deleted)} 处「已删/生成物」备注："
          f"{sorted(set(skipped_deleted))[:4]}）")
if bad:
    print(f"\n⚠ 找不到的 {len(bad)} 条：")
    for name, s in bad:
        print(f"  [{name}] {s}")
else:
    print("✅ 全部引用都能对上真实文件")
