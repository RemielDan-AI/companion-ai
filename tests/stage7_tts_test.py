#!/usr/bin/env python
"""阶段 7 TTS 接口测试。

A. 离线：用一个本地 mock 服务验证协议客户端——v2(POST)、v1(GET)、自动回退、
   OpenAI 兼容、命令行兜底、文本清洗、health 探测、落盘是否是可播放的 wav
B. 在线：按 config/voice.yaml 探测你本地的 GSV 服务；不可达只提示，不算失败

用法：python tests/stage7_tts_test.py
"""
from __future__ import annotations

import io
import json
import pathlib
import shutil
import struct
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voice import tts as T  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp"


def make_wav(seconds: float = 0.2, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


WAV = make_wav()


class Handler(BaseHTTPRequestHandler):
    mode = "v2"          # v2 | v1 | openai
    last_body = b""

    def log_message(self, *args):  # 静音
        pass

    def _audio(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(WAV)))
        self.end_headers()
        self.wfile.write(WAV)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        Handler.last_body = self.rfile.read(length)
        if self.mode == "openai" and self.path.startswith("/v1/audio/speech"):
            return self._audio()
        if self.mode == "v2" and self.path.startswith("/tts"):
            return self._audio()
        self.send_response(405)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/openapi.json"):
            payload = json.dumps({"paths": {"/tts": {}, "/docs": {}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.mode == "v1" and self.path.startswith("/tts"):
            return self._audio()
        self.send_response(404)
        self.end_headers()


def serve(mode: str) -> tuple[ThreadingHTTPServer, int]:
    handler = type(f"Handler_{mode}", (Handler,), {"mode": mode})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def check_wav(path: pathlib.Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "文件不存在"
    data = path.read_bytes()
    if data[:4] != b"RIFF":
        return False, f"不是 WAV（{data[:4]!r}）"
    with wave.open(str(path), "rb") as fh:
        frames = fh.getnframes()
        rate = fh.getframerate()
    return frames > 0 and rate > 0, f"{frames} 帧 @ {rate}Hz"


def check_audio(path: pathlib.Path) -> tuple[bool, str]:
    """通用校验：只要是能播的音频就算通过（GSV-TTS-Lite 固定返回 Ogg Vorbis）。"""
    if not path.exists():
        return False, "文件不存在"
    data = path.read_bytes()
    if not T.looks_like_audio(data):
        return False, f"不是音频（{data[:8]!r}）"
    if data[:4] == b"RIFF":
        return check_wav(path)
    container = {b"OggS": "Ogg", b"ID3": "MP3", b"fLaC": "FLAC"}.get(data[:4], "未知")
    detail = f"{container} {len(data)/1024:.0f} KB"
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                                  "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            if out:
                detail += f"，{float(out):.1f} 秒"
        except Exception:
            pass
    return True, detail


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    out_dir = TMP / "tts_out"
    shutil.rmtree(out_dir, ignore_errors=True)
    checks: list[tuple[str, bool, str]] = []

    # ---- 文本清洗 ----
    dirty = "# 标题\n\n**加粗** 和 `代码` 还有表情😀 换行\n都去掉"
    cleaned = T.clean_text(dirty, 100)
    checks.append(("文本清洗（markdown/换行/表情）",
                   "#" not in cleaned and "*" not in cleaned and "\n" not in cleaned
                   and "😀" not in cleaned and "加粗" in cleaned, cleaned))
    long_text = "第一句。" * 60
    checks.append(("超长截断在句号处",
                   len(T.clean_text(long_text, 40)) <= 42
                   and T.clean_text(long_text, 40).endswith("。"), ""))
    checks.append(("音频魔数判断", T.looks_like_audio(WAV)
                   and not T.looks_like_audio(b'{"detail":"error"}'), ""))

    # ---- v2（POST）----
    server_v2, port_v2 = serve("v2")
    cfg = {"backend": "gsv", "output_dir": str(out_dir), "text_max_chars": 100,
           "gsv": {"base_url": f"http://127.0.0.1:{port_v2}", "api": "auto",
                   "ref_audio_path": "ref.wav", "prompt_text": "你好",
                   "extra": {"top_k": 8}}}
    try:
        path = T.synthesize("嗯，我在呢。", cfg, name="v2test")
        ok, detail = check_wav(path)
        checks.append(("v2 POST 合成并落盘为有效 WAV", ok, detail))
        body = json.loads(Handler.last_body.decode("utf-8"))
        checks.append(("请求体带上了参考音频与参数",
                       body.get("ref_audio_path") == "ref.wav"
                       and body.get("top_k") == 8 and body.get("text") == "嗯，我在呢。",
                       str(list(body.keys()))))
        info = T.health(cfg)
        checks.append(("health 能识别服务与接口",
                       info["ok"] and "/tts" in info.get("endpoints", []), info["detail"]))
    except T.TtsError as exc:
        checks.append(("v2 POST 合成并落盘为有效 WAV", False, str(exc)))
    server_v2.shutdown()

    # ---- v1（POST 失败自动回退 GET）----
    server_v1, port_v1 = serve("v1")
    cfg_v1 = {"backend": "gsv", "output_dir": str(out_dir),
              "gsv": {"base_url": f"http://127.0.0.1:{port_v1}", "api": "auto",
                      "ref_audio_path": "ref.wav", "prompt_text": "你好"}}
    try:
        path = T.synthesize("回退测试。", cfg_v1, name="v1test")
        ok, detail = check_wav(path)
        checks.append(("v2 不可用时自动回退 v1(GET)", ok, detail))
    except T.TtsError as exc:
        checks.append(("v2 不可用时自动回退 v1(GET)", False, str(exc)))
    server_v1.shutdown()

    # ---- OpenAI 兼容 ----
    server_oa, port_oa = serve("openai")
    cfg_oa = {"backend": "openai", "output_dir": str(out_dir),
              "openai": {"base_url": f"http://127.0.0.1:{port_oa}/v1/audio/speech"}}
    try:
        path = T.synthesize("OpenAI 兼容测试。", cfg_oa, name="oatest")
        ok, detail = check_wav(path)
        checks.append(("OpenAI 兼容接口可用", ok, detail))
    except T.TtsError as exc:
        checks.append(("OpenAI 兼容接口可用", False, str(exc)))
    server_oa.shutdown()

    # ---- 命令行兜底 ----
    script = (f"{sys.executable} -c \"import wave;w=wave.open(r'{{out}}','wb');"
              "w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);"
              "w.writeframes(b'\\x00\\x00'*1600);w.close()\"")
    cfg_cmd = {"backend": "command", "output_dir": str(out_dir),
               "command": {"template": script}}
    try:
        path = T.synthesize("命令行兜底测试。", cfg_cmd, name="cmdtest")
        ok, detail = check_wav(path)
        checks.append(("命令行兜底后端可用", ok, detail))
    except T.TtsError as exc:
        checks.append(("命令行兜底后端可用", False, str(exc)))

    # ---- 默认配置能被解析 ----
    default_cfg = T.load_config()
    checks.append(("config/voice.yaml 可解析",
                   isinstance(default_cfg, dict) and "backend" in default_cfg,
                   f"backend={default_cfg.get('backend')}"))

    passed = sum(1 for _, ok, _ in checks if ok)
    print("=" * 66)
    print("A. 离线协议测试（mock 服务）")
    print("=" * 66)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"A. 通过 {passed}/{len(checks)}\n")

    # ---- B. 真实服务探测（尽力而为）----
    print("=" * 66)
    print("B. 本地 GSV 服务探测")
    print("=" * 66)
    real_cfg = dict(default_cfg)
    real_cfg["output_dir"] = str(TMP / "tts_live")
    info = T.health(real_cfg)
    print(f"后端 {info['backend']}　可用 {info['ok']}　{info['detail']}")
    live_ok = None
    if info["ok"]:
        try:
            path = T.synthesize("嗯，我在呢。声音测试。", real_cfg, name="livetest")
            live_ok, detail = check_audio(path)
            print(f"{'✓' if live_ok else '✗'} 真实合成　{path.name}　{detail}")
        except T.TtsError as exc:
            live_ok = False
            print(f"✗ 真实合成失败：{exc}")
    else:
        print("（本地 GSV 未启动或地址未配置——启动后把 config/voice.yaml 的 gsv.base_url 指过去，"
              "再跑一次本测试即可验证）")

    shutil.rmtree(TMP, ignore_errors=True)
    total_ok = passed == len(checks) and live_ok is not False
    return 0 if total_ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
