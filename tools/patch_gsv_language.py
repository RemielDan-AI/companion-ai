#!/usr/bin/env python
"""给 GSV-TTS-Lite 的 personal_api.py 打补丁：让它把语言参数真正传下去。

背景：这个服务的 auto 自动识别其实很准（实测 日文→ja、中文→zh、英文→en），
      所以日文文本本来就能正确朗读。但 /tts 接收的 text_lang / prompt_lang
      不会往下传，于是"语言下拉框"永远不起作用——想强制指定读音（例如让
      中文文本按日语读）就必须打这个补丁。

问题位置：personal_api.py 里 tts.infer_async(...) 的调用没传 text_language /
      prompt_language（库函数本身是支持的，默认 auto）。

补丁：补上
      text_language=req.get("text_lang") or "auto"
      prompt_language=req.get("prompt_lang") or "auto"

特征：幂等（已打过就跳过）、自动备份、只在匹配到唯一目标时改动、可一键还原。

用法：
    python tools/patch_gsv_language.py --check     # 只看现状，不改
    python tools/patch_gsv_language.py             # 打补丁（之后需重启服务）
    python tools/patch_gsv_language.py --revert    # 从备份还原
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
import time

API_PATH = pathlib.Path(r"C:\TTS\GSV-TTS-Lite\API\personal_api.py")

TARGET = """    clip = await tts.infer_async(
        spk_audio_path=speaker_audio,
        prompt_audio_path=prompt_audio,
        prompt_audio_text=prompt_text,
        text=text_input,
"""

PATCHED = """    clip = await tts.infer_async(
        spk_audio_path=speaker_audio,
        prompt_audio_path=prompt_audio,
        prompt_audio_text=prompt_text,
        text=text_input,
        # ↓ 本行与下一行是补丁：原版没把语言传下去，日语会被当中文读
        text_language=req.get("text_lang") or "auto",
        prompt_language=req.get("prompt_lang") or "auto",
"""

MARK = 'text_language=req.get("text_lang")'


def read_text() -> str:
    return API_PATH.read_text(encoding="utf-8")


def status() -> str:
    if not API_PATH.exists():
        return f"找不到 {API_PATH}"
    text = read_text()
    if MARK in text:
        return "已打补丁"
    if TARGET in text:
        return "未打补丁（可以打）"
    return "文件结构与预期不符（版本可能不同），未改动"


def backup() -> pathlib.Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = API_PATH.with_suffix(f".py.bak-{stamp}")
    shutil.copy2(API_PATH, path)
    return path


def latest_backup() -> pathlib.Path | None:
    backups = sorted(API_PATH.parent.glob(API_PATH.name + ".bak-*"))
    return backups[-1] if backups else None


def apply_patch() -> int:
    text = read_text()
    if MARK in text:
        print("已经是打过补丁的状态，无需改动")
        return 0
    if TARGET not in text:
        print("没找到预期代码块，未改动（可能是别的版本）")
        return 1
    bak = backup()
    print(f"已备份：{bak.name}")
    API_PATH.write_text(text.replace(TARGET, PATCHED, 1), encoding="utf-8")
    print("已打补丁：infer_async 现在会收到 text_language / prompt_language")
    print("需要重启 GSV 服务才会生效（C:\\TTS\\GSV-TTS-Lite\\api启动.bat）")
    return 0


def revert() -> int:
    bak = latest_backup()
    if not bak:
        print("没有备份可还原")
        return 1
    shutil.copy2(bak, API_PATH)
    print(f"已从 {bak.name} 还原")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GSV-TTS-Lite 语言参数补丁")
    parser.add_argument("--check", action="store_true", help="只看现状")
    parser.add_argument("--revert", action="store_true", help="从最新备份还原")
    args = parser.parse_args()

    print(f"目标：{API_PATH}")
    print(f"现状：{status()}")
    if args.check:
        return 0
    if args.revert:
        return revert()
    return apply_patch()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
