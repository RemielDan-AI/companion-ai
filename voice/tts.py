"""语音合成（TTS）：只做接口兼容，不内置任何模型。

四种后端：
- gsv     本地 GPT-SoVITS / GSV。同时兼容 api_v2.py 的 POST /tts 与 api.py 的 GET /tts
- edge    Edge TTS（微软在线，需要联网，声音可选）
- openai  OpenAI 兼容的 /v1/audio/speech
- command 任意命令行 TTS，模板里 {text} / {out} 占位

合成为音频文件落到 output_dir，返回路径。失败抛 TtsError，调用方自己决定降级。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "voice.yaml"

# 音频文件的魔数，用来判断服务到底返回了音频还是错误 JSON
_MAGIC = (b"RIFF", b"ID3", b"OggS", b"fLaC", b"\xff\xfb", b"\xff\xf3", b"ftyp")


class TtsError(RuntimeError):
    pass


def load_config(path: pathlib.Path | str = CONFIG_PATH) -> dict:
    path = pathlib.Path(path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def looks_like_audio(data: bytes) -> bool:
    if not data or len(data) < 64:
        return False
    head = data[:12]
    return any(head.startswith(magic) for magic in _MAGIC)


def clean_text(text: str, limit: int = 300) -> str:
    """喂给 TTS 之前先洗一遍：去 markdown、去换行、去表情，只留能读出来的话。"""
    text = str(text or "")
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)      # 代码块
    text = re.sub(r"[*_`#>~\[\]()]", "", text)                    # markdown 符号
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", text)  # 表情
    text = re.sub(r"\s+", " ", text)                              # 换行/多余空格
    text = text.strip()
    if limit and len(text) > limit:
        cut = max(text.rfind("。", 0, limit), text.rfind("？", 0, limit),
                  text.rfind("！", 0, limit))
        text = text[:cut + 1] if cut > limit * 0.5 else text[:limit]
    return text


def _http(url: str, data: bytes | None = None, headers: dict | None = None,
          timeout: int = 120, method: str | None = None) -> tuple[int, bytes, dict]:
    request = urllib.request.Request(url, data=data, headers=headers or {},
                                     method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", dict(exc.headers or {})
    except urllib.error.URLError as exc:
        raise TtsError(f"连不上 {url}（{exc}）") from exc


# ---------------- GSV（GPT-SoVITS） ----------------
def _gsv_v2(text: str, cfg: dict) -> bytes:
    payload = {
        "text": text,
        "text_lang": cfg.get("text_lang", "zh"),
        "ref_audio_path": cfg.get("ref_audio_path", ""),
        "prompt_text": cfg.get("prompt_text", ""),
        "prompt_lang": cfg.get("prompt_lang", "zh"),
        "text_split_method": cfg.get("text_split_method", "cut5"),
        "batch_size": 1,
        "media_type": cfg.get("media_type", "wav"),
        "streaming_mode": bool(cfg.get("streaming_mode", False)),
        "speed_factor": cfg.get("speed", 1.0),
    }
    payload.update(cfg.get("extra") or {})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, data, _ = _http(cfg["base_url"].rstrip("/") + "/tts", data=body,
                            headers={"Content-Type": "application/json"},
                            timeout=int(cfg.get("timeout", 120)))
    if status != 200 or not looks_like_audio(data):
        raise TtsError(f"v2 /tts 返回 {status}，内容不是音频：{data[:120]!r}")
    return data


def _gsv_v1(text: str, cfg: dict) -> bytes:
    query = urllib.parse.urlencode({
        "text": text,
        "text_language": cfg.get("text_language", "zh"),
        "refer_wav_path": cfg.get("ref_audio_path", ""),
        "prompt_text": cfg.get("prompt_text", ""),
        "prompt_language": cfg.get("prompt_language", "zh"),
        "top_k": (cfg.get("extra") or {}).get("top_k", 5),
        "top_p": (cfg.get("extra") or {}).get("top_p", 1),
        "temperature": (cfg.get("extra") or {}).get("temperature", 1),
        "speed": cfg.get("speed", 1.0),
        "cut_punc": (cfg.get("extra") or {}).get("cut_punc", ""),
    })
    status, data, _ = _http(cfg["base_url"].rstrip("/") + "/tts?" + query,
                            timeout=int(cfg.get("timeout", 120)))
    if status != 200 or not looks_like_audio(data):
        raise TtsError(f"v1 /tts 返回 {status}，内容不是音频：{data[:120]!r}")
    return data


def gsv_synthesize(text: str, cfg: dict) -> bytes:
    api = str(cfg.get("api", "auto")).lower()
    errors = []
    attempts = [("v2", _gsv_v2), ("v1", _gsv_v1)] if api == "auto" else \
               [(api, _gsv_v2 if api == "v2" else _gsv_v1)]
    for name, func in attempts:
        try:
            return func(text, cfg)
        except (TtsError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
    raise TtsError("；".join(errors) or "GSV 合成失败")


# ---------------- Edge TTS（微软在线，需要联网） ----------------
VOICE_CACHE = ROOT / "voice" / "edge_voices.json"
EDGE_FALLBACK_VOICES = [
    {"name": "zh-CN-XiaoxiaoNeural", "gender": "Female", "locale": "zh-CN", "note": "温暖"},
    {"name": "zh-CN-XiaoyiNeural", "gender": "Female", "locale": "zh-CN", "note": "活泼"},
    {"name": "zh-CN-YunxiNeural", "gender": "Male", "locale": "zh-CN", "note": "阳光"},
    {"name": "zh-CN-YunjianNeural", "gender": "Male", "locale": "zh-CN", "note": "有力"},
    {"name": "zh-CN-YunyangNeural", "gender": "Male", "locale": "zh-CN", "note": "播报"},
    {"name": "zh-CN-liaoning-XiaobeiNeural", "gender": "Female", "locale": "zh-CN",
     "note": "东北话"},
]


def edge_available() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return shutil.which("edge-tts") is not None


def list_edge_voices(locale_prefix: str = "zh-", refresh: bool = False) -> list[dict]:
    """列出 Edge 声音。优先在线拉取，失败用本地缓存，再不行用内置短名单。"""
    voices: list[dict] | None = None
    if VOICE_CACHE.exists() and not refresh:
        try:
            voices = json.loads(VOICE_CACHE.read_text(encoding="utf-8"))
        except Exception:
            voices = None
    if not voices:
        try:
            import asyncio

            import edge_tts
            raw = asyncio.run(edge_tts.list_voices())
            voices = []
            for item in raw:
                name = item.get("ShortName", "")
                if not name:
                    continue
                tag = item.get("VoiceTag") if isinstance(item.get("VoiceTag"), dict) else {}
                personalities = (tag or {}).get("VoicePersonalities") or [""]
                voices.append({"name": name, "gender": item.get("Gender", ""),
                               "locale": item.get("Locale", ""),
                               "note": personalities[0] if personalities else ""})
            VOICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            VOICE_CACHE.write_text(json.dumps(voices, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        except Exception:
            voices = None
    if not voices:
        voices = EDGE_FALLBACK_VOICES
    if locale_prefix:
        filtered = [v for v in voices if v["name"].startswith(locale_prefix)]
        voices = filtered or voices
    # zh-CN 排前面，再按"女声/男声 + 名字"，下拉列表更好找
    def sort_key(item: dict):
        name = item.get("name", "")
        locale_rank = 0 if name.startswith("zh-CN") else (1 if name.startswith("zh-") else 2)
        gender_rank = 0 if item.get("gender") == "Female" else 1
        return (locale_rank, gender_rank, name)

    return sorted(voices, key=sort_key)


def edge_synthesize(text: str, cfg: dict, out_path: pathlib.Path) -> bytes:
    voice = cfg.get("voice") or "zh-CN-XiaoxiaoNeural"
    rate = cfg.get("rate", "+0%")
    volume = cfg.get("volume", "+0%")
    pitch = cfg.get("pitch", "+0Hz")
    try:
        import asyncio

        import edge_tts
        communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
        asyncio.run(communicate.save(str(out_path)))
    except ImportError:
        if not shutil.which("edge-tts"):
            raise TtsError("没装 edge-tts（pip install edge-tts）")
        result = subprocess.run(
            ["edge-tts", "--text", text, "--voice", voice,
             "--rate", rate, "--volume", volume, "--pitch", pitch,
             "--write-media", str(out_path)],
            capture_output=True, timeout=int(cfg.get("timeout", 120)))
        if result.returncode != 0:
            raise TtsError(f"edge-tts 命令失败：{result.stderr[:160]!r}")
    except Exception as exc:
        raise TtsError(f"Edge TTS 合成失败（需要联网）：{exc}") from exc
    if not out_path.exists() or out_path.stat().st_size < 512:
        raise TtsError("Edge TTS 没有产出音频")
    return out_path.read_bytes()


def save_ui_state(state: dict) -> None:
    """窗口里选的语音设置另存一份（不动带注释的 config/voice.yaml）。"""
    path = ROOT / "voice" / "ui_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def load_ui_state() -> dict:
    path = ROOT / "voice" / "ui_state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------- OpenAI 兼容 ----------------
def openai_synthesize(text: str, cfg: dict) -> bytes:
    payload = {"model": cfg.get("model", "tts-1"), "input": text,
               "voice": cfg.get("voice", "alloy"), "response_format": "wav"}
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    status, data, _ = _http(cfg["base_url"], data=json.dumps(payload).encode("utf-8"),
                            headers=headers, timeout=int(cfg.get("timeout", 120)))
    if status != 200 or not looks_like_audio(data):
        raise TtsError(f"/v1/audio/speech 返回 {status}：{data[:120]!r}")
    return data


# ---------------- 命令行兜底 ----------------
def command_synthesize(text: str, cfg: dict, out_path: pathlib.Path) -> bytes:
    template = str(cfg.get("template") or "").strip()
    if not template:
        raise TtsError("command 后端没配 template")
    command = template.replace("{text}", text).replace("{out}", str(out_path))
    result = subprocess.run(command, shell=True, capture_output=True,
                            timeout=int(cfg.get("timeout", 120)))
    if result.returncode != 0 or not out_path.exists():
        raise TtsError(f"命令失败（{result.returncode}）：{result.stderr[:160]!r}")
    return out_path.read_bytes()


# ---------------- 对外接口 ----------------
def synthesize(text: str, cfg: dict | None = None, play: bool = False,
               name: str | None = None) -> pathlib.Path:
    """合成一段话，返回音频文件路径。"""
    cfg = cfg if cfg is not None else load_config()
    spoken = clean_text(text, int(cfg.get("text_max_chars", 300)))
    if not spoken:
        raise TtsError("要合成的内容是空的")

    out_dir = pathlib.Path(cfg.get("output_dir", "voice/out"))
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = name or time.strftime("%Y%m%d-%H%M%S") + f"-{abs(hash(spoken)) % 1000:03d}"

    backend = str(cfg.get("backend", "gsv")).lower()
    if backend == "edge":
        # Edge TTS 直接产出 mp3，由它自己写盘
        path = out_dir / f"{stamp}.mp3"
        edge_synthesize(spoken, cfg.get("edge") or {}, path)
        if play:
            play_file(path)
        return path

    if backend == "gsv":
        data = gsv_synthesize(spoken, cfg.get("gsv") or {})
    elif backend == "openai":
        data = openai_synthesize(spoken, cfg.get("openai") or {})
    elif backend == "command":
        data = command_synthesize(spoken, cfg.get("command") or {}, out_dir / f"{stamp}.wav")
    else:
        raise TtsError(f"未知后端：{backend}")

    suffix = ".wav"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3"):
        suffix = ".mp3"
    elif data[:4] == b"OggS":
        suffix = ".ogg"
    path = out_dir / f"{stamp}{suffix}"
    path.write_bytes(data)
    if play:
        play_file(path)
    return path


_PLAYER_LOCK = threading.Lock()
_CURRENT_PLAYER: subprocess.Popen | None = None


def stop_playback() -> bool:
    """停掉当前正在播放的声音。返回是否真的停了一个。"""
    global _CURRENT_PLAYER
    with _PLAYER_LOCK:
        proc = _CURRENT_PLAYER
        _CURRENT_PLAYER = None
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True
    except Exception:
        return False


def is_playing() -> bool:
    """当前是否有声音在播。"""
    with _PLAYER_LOCK:
        proc = _CURRENT_PLAYER
    return proc is not None and proc.poll() is None


def play_file(path: pathlib.Path | str, stop_current: bool = True):
    """在本机播放音频，返回播放进程（系统播放器回退时返回 None）。

    同一时刻只允许一段声音：默认先停掉正在播的那段，避免自动朗读和回听叠在一起。
    优先用 ffplay（无窗口、支持 ogg/opus 等任意格式）；没有才回退系统默认播放器。
    """
    global _CURRENT_PLAYER
    path = str(path)
    if stop_current:
        stop_playback()
    try:
        ffplay = shutil.which("ffplay")
        if ffplay:
            proc = subprocess.Popen(
                [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with _PLAYER_LOCK:
                _CURRENT_PLAYER = proc
            return proc
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
            return None
        for player in ("aplay", "afplay"):
            if shutil.which(player):
                proc = subprocess.Popen([player, path],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                with _PLAYER_LOCK:
                    _CURRENT_PLAYER = proc
                return proc
        return None
    except Exception:
        return None


def health(cfg: dict | None = None) -> dict:
    """探测服务是否可用、暴露了哪些接口。用于排障，不抛异常。"""
    cfg = cfg if cfg is not None else load_config()
    backend = str(cfg.get("backend", "gsv")).lower()
    result = {"backend": backend, "ok": False, "detail": "", "endpoints": []}

    if backend == "gsv":
        gsv = cfg.get("gsv") or {}
        base = str(gsv.get("base_url", "")).rstrip("/")
        if not base:
            result["detail"] = "没配 base_url"
            return result
        try:
            status, data, _ = _http(base + "/openapi.json", timeout=6)
            if status == 200:
                try:
                    schema = json.loads(data.decode("utf-8"))
                    result["endpoints"] = sorted(schema.get("paths", {}).keys())[:20]
                except Exception:
                    pass
                result["ok"] = True
                result["detail"] = f"服务在线，接口 {result['endpoints'] or '未知'}"
            else:
                status, data, _ = _http(base + "/", timeout=6)
                result["ok"] = status < 500
                result["detail"] = f"根路径返回 {status}"
        except TtsError as exc:
            result["detail"] = str(exc)
        return result

    if backend == "edge":
        ok = edge_available()
        voices = len(list_edge_voices("")) if ok else 0
        result["ok"] = ok
        result["detail"] = (f"edge-tts 已安装，可用声音 {voices} 个（合成需要联网）"
                            if ok else "没装 edge-tts：pip install edge-tts")
        return result

    if backend == "openai":
        url = str((cfg.get("openai") or {}).get("base_url", ""))
        try:
            status, _, _ = _http(url, data=b"{}", timeout=6,
                                 headers={"Content-Type": "application/json"})
            result["ok"] = status < 500
            result["detail"] = f"返回 {status}（405/400 也说明服务在线）"
        except TtsError as exc:
            result["detail"] = str(exc)
        return result

    result["ok"] = bool((cfg.get("command") or {}).get("template"))
    result["detail"] = "命令行后端" + ("已配置" if result["ok"] else "未配置 template")
    return result
