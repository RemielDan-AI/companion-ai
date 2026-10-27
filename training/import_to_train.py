"""把记忆 SFT 样本**安全地**追加进训练集源文件。

## 为什么不能直接 `Add-Content`

⚠ **实测踩到的坑**（`a.py` 的自校验当场报出来）：

    weiyang_train: 1085 → 1158   ← 应该 +74，少了一条
    第 396 行解析失败：Extra data    ← 两条 JSON 粘在一行

原因：`微调数据/主要数据/weiyang_train.jsonl` 的**最后一行没有换行符**
（`general_120.jsonl` 也一样，实测末尾字节都是 `}`），
直接往后追加会让**它最后一条和新文件第一条粘成一行**——
**少一条样本 + 一行无法解析**（训练脚本会报错或静默丢样本）。

`a.py` 的注释里早就记过这个坑（`writelines` 不补换行），
但那只修了**混合**那一步，**追加**这一步还得自己当心。

## 这个脚本做的事

    ① 备份目标文件（`*.bak-YYYYmmdd-HHMMSS`）
    ② **先补齐末尾换行**，再逐行追加
    ③ 逐行校验 JSON 能否解析、条数是否 = 原有 + 新增
    ④ 有任何一条坏 → **回滚到备份**，不留半成品

用法：
    python training/import_to_train.py --dry-run          # 只看会做什么
    python training/import_to_train.py                    # 真追加
    python training/import_to_train.py --repeat 3         # 过采样 3 倍（提权重）
    python training/import_to_train.py --target <路径>     # 换目标文件
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SRC = ROOT / "training" / "export" / "memory_sft.jsonl"
DEFAULT_TARGET = pathlib.Path(r"./微调数据\主要数据\weiyang_train.jsonl")


def count_lines(path: pathlib.Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def all_parse(path: pathlib.Path) -> list[int]:
    """返回**解析失败**的行号列表。"""
    bad = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad.append(i)
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument("--repeat", type=int, default=1,
                    help="过采样倍数（同一批重复几次，提高它在训练集里的权重）")
    ap.add_argument("--replace-source", default="",
                    help="**先删掉训练集里 source 等于这个值的旧样本**，再追加。"
                         "⚠ 重新生成样本后必须带它，否则会叠加成两份"
                         "（实测第二次会变成 222+429=651 条）。")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = pathlib.Path(args.src)
    target = pathlib.Path(args.target)
    if not src.exists():
        print(f"✗ 找不到样本文件 {src}")
        return 1
    if not target.exists():
        print(f"✗ 找不到训练集 {target}")
        return 1

    new = [l for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    for i, line in enumerate(new, 1):
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"✗ 样本第 {i} 行本身就不合法：{exc}")
            return 1
    new = new * max(1, args.repeat)

    # ---- 可选：先把旧的那批记忆样本摘掉（source 匹配）----
    kept: list[str] = []
    removed = 0
    if args.replace_source:
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)      # 坏行原样留着，交给后面的校验去报
                continue
            if str(row.get("source") or "") == args.replace_source:
                removed += 1
            else:
                kept.append(line)
        before = len(kept)
        print(f"  --replace-source {args.replace_source}：**摘掉旧样本 {removed} 条**")
        print(f"  摘完后应为 {before} 条，再加 {len(new)} 条 → {before + len(new)} 条")
    else:
        before = count_lines(target)

    ends_nl = target.read_bytes().endswith(b"\n")
    print(f"目标：{target}")
    print(f"  原有 {before} 条　末尾换行={ends_nl}（**False 就是那个坑**）")
    print(f"  将追加 {len(new)} 条（样本 {len(new)//max(1,args.repeat)} 条 × {args.repeat} 倍）")
    print(f"  追加后应为 {before + len(new)} 条")

    if not ends_nl:
        print("  ⚠ 末尾没有换行 → **先补一个换行再追加**（否则会粘行、少一条）")
    if args.dry_run:
        print("\n（--dry-run：没有动任何文件）")
        return 0

    backup = target.with_name(
        target.name + f".bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(target, backup)
    print(f"  已备份 → {backup.name}")

    try:
        # ⚠ 带 --replace-source 时**整体重写**（先摘旧再写新），
        # ⚠ 带 --replace-source 时**整体重写**（先摘旧再写新）；
        #   不带时才是纯追加（append，不会碰已有内容）。
        if args.replace_source:
            with target.open("w", encoding="utf-8") as fh:
                for line in kept:
                    fh.write(line.rstrip("\n") + "\n")
                for line in new:
                    fh.write(line.rstrip("\n") + "\n")
        else:
            with target.open("a", encoding="utf-8") as fh:
                if not ends_nl:
                    fh.write("\n")
                for line in new:
                    fh.write(line.rstrip("\n") + "\n")
        # ---- 校验：解析 + 条数 ----
        bad = all_parse(target)
        after = count_lines(target)
        ok = (not bad) and after == before + len(new)
        print(f"  校验：条数 {after}（期望 {before + len(new)}）"
              f"{'✅' if after == before + len(new) else '⚠'}；"
              f"坏行 {len(bad)}{'✅' if not bad else '✗'}")
        if not ok:
            shutil.copy2(backup, target)
            print(f"  ✗ 校验没过，**已回滚**到备份（坏行：{bad[:5]}）")
            return 1
    except Exception as exc:
        shutil.copy2(backup, target)
        print(f"  ✗ 出错已回滚：{type(exc).__name__}: {exc}")
        return 1

    print()
    print("✅ 追加完成。下一步（**每次微调前都要做**）：")
    print(f"    cd \"{target.parent}\" && python a.py")
    print("    它会把 weiyang_train.jsonl + general_120.jsonl 打乱混合成")
    print("    weiyang_train_mixed.jsonl，并自校验逐行可解析、条数对得上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
