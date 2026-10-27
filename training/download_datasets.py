"""数据集下载（走 hf-mirror.com，huggingface.co 直连不通）。

用法：
    python training/download_datasets.py --list          # 只看有哪些文件、多大
    python training/download_datasets.py                 # 全部下载
    python training/download_datasets.py --only DeepSupport
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import urllib.request

MIRROR = "https://hf-mirror.com"
TARGET = pathlib.Path(r"./微调数据\datasets")

# 数据收集.txt 里列的（最后两条是原创 QA，不需要下载）
DATASETS = [
    ("Muice-Dataset", "Moemu/Muice-Dataset", "可用但需要大量改写"),
    ("DeepSupport", "Yukyin/DeepSupport", "可用"),
    ("deep-emotional-support-zh", "AngelWarmSmile123/deep-emotional-support-zh",
     "可用，需要筛查、格式要调整"),
    ("kaidol-roleplay-v2", "developer-lunark/kaidol-roleplay-v2", "待筛查"),
    ("chinese-adorable-high-eq-chat",
     "MemorialSummer/chinese-adorable-high-emotional-intelligence-chat", "可用但需改写"),
]

# 防遗忘子集的真实仓库名（上面只是占位说明）
FORGETTING = [
    ("BELLE-multiturn-chat-0.8M", "BelleGroup/multiturn_chat_0.8M"),
    ("ShareGPT-Chinese-English-90k", "shareAI/ShareGPT-Chinese-English-90k"),
]

DATA_SUFFIX = (".parquet", ".json", ".jsonl", ".csv", ".arrow", ".txt")
SKIP_HINTS = ("image", "audio", "wav", "mp3", "png", "jpg", "video", "thumb")


def repo_files(repo: str, kind: str = "datasets") -> list[dict]:
    """列出仓库文件（含大小）。用 curl 查，镜像对空 UA 的请求会 403。"""
    url = f"{MIRROR}/api/{kind}/{repo}?blobs=true"
    try:
        proc = subprocess.run(
            ["curl.exe", "-sL", "--max-time", "60",
             "-H", "User-Agent: Mozilla/5.0 (companion-ai dataset fetcher)", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        raw = (proc.stdout or "").strip()
        if not raw:
            return [{"error": (proc.stderr or "空响应").strip()[:120]}]
        data = json.loads(raw)
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    if isinstance(data, dict) and data.get("error"):
        return [{"error": str(data["error"])[:120]}]
    out = []
    for item in (data.get("siblings") or []):
        out.append({"name": item.get("rfilename") or "", "size": item.get("size") or 0})
    return out


def pick(files: list[dict], per_repo_limit: int = 6) -> list[dict]:
    """挑出数据文件：优先 parquet，再 json/jsonl；跳过图片音频。"""
    data_files = [f for f in files if str(f["name"]).lower().endswith(DATA_SUFFIX)
                  and not any(hint in str(f["name"]).lower() for hint in SKIP_HINTS)]
    data_files.sort(key=lambda f: (0 if f["name"].endswith(".parquet") else 1, f["name"]))
    return data_files[:per_repo_limit]


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def main() -> int:
    parser = argparse.ArgumentParser(description="下载微调数据集")
    parser.add_argument("--list", action="store_true", help="只看文件与大小")
    parser.add_argument("--only", default="", help="只下载名字里包含这个字符串的仓库")
    parser.add_argument("--per-repo", type=int, default=6, help="每个仓库最多下几个文件")
    args = parser.parse_args()

    targets = list(DATASETS[:5]) + [(n, r, "防遗忘子集") for n, r in FORGETTING]
    total = 0
    plan = []
    for label, repo, note in targets:
        if args.only and args.only.lower() not in label.lower() \
                and args.only.lower() not in repo.lower():
            continue
        files = repo_files(repo)
        if files and files[0].get("error"):
            print(f"! {label}（{repo}）查询失败：{files[0]['error']}")
            continue
        chosen = pick(files, args.per_repo)
        size = sum(f["size"] for f in chosen)
        total += size
        print(f"{label}　{repo}　{note}")
        for f in chosen:
            print(f"    {human(f['size']):>9}  {f['name']}")
        print(f"    小计 {human(size)}　（仓库共 {len(files)} 个文件）")
        plan.append((label, repo, chosen))

    print(f"\n计划下载合计：{human(total)}")
    if args.list:
        return 0

    TARGET.mkdir(parents=True, exist_ok=True)
    for label, repo, chosen in plan:
        out_dir = TARGET / label
        out_dir.mkdir(parents=True, exist_ok=True)
        for f in chosen:
            dest = out_dir / pathlib.Path(f["name"]).name
            if dest.exists() and dest.stat().st_size == f["size"] and f["size"]:
                print(f"已存在，跳过：{dest.name}")
                continue
            url = f"{MIRROR}/datasets/{repo}/resolve/main/{f['name']}"
            print(f"下载 {label}/{dest.name}（{human(f['size'])}）…")
            subprocess.run(["curl.exe", "-L", "-C", "-", "--retry", "3",
                            "--retry-delay", "5", "-o", str(dest), url], check=False)
    print("\n完成，目录：", TARGET)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
