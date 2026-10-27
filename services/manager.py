"""本地模型服务的进程管理（项目改进v1 模块四）。

解决的问题：
- 统一启动：对话模型、嵌入模型都用同一个管理器拉起（原来是三个 .bat 各开一个窗口）；
- 后台无窗口：llama-server 用 CREATE_NO_WINDOW 启动，不再弹控制台；
- 关闭前台即退出：所有子进程挂在 Win32 Job Object 上，前台进程一关就一起收掉，
  不留残留进程（没有 Job Object 时退化为退出钩子里逐个 terminate）；
- 启动可诊断：端口占用、模型文件缺失、加载失败、显存不足都能给出明确原因；
- 启动日志：每次启动记录时间、模型、参数、PID（logs/services.jsonl）；
- 可选模型与参数：模型列表自动扫描，参数按需拼命令，配合 services/presets.py 存预设。

平台：Windows 优先；其它平台退化为常规 Popen（无 Job Object）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
START_LOG = LOG_DIR / "services.jsonl"
# 默认模型目录。可用环境变量 `COMPANION_MODEL_DIRS` 覆盖（多个用 ; 分隔）。
# 开源版特意做成可配置：别人的模型不会都在同一个固定位置。
_env_roots = os.environ.get("COMPANION_MODEL_DIRS", "").strip()
DEFAULT_MODEL_ROOTS = (
    [pathlib.Path(x.strip()) for x in _env_roots.replace(";", os.pathsep).split(os.pathsep) if x.strip()]
    or [pathlib.Path.home() / "ai" / "models", pathlib.Path("models")]
)

# 对话模型的默认参数：按 4 GB 显存实测调过的（ngl=20 / ctx=4096）
CHAT_DEFAULTS = {
    "kind": "chat", "host": "127.0.0.1", "port": 8080, "ctx": 4096, "ngl": 15,
    "threads": 8, "kv": "q8_0", "batch": 128, "ubatch": 64, "parallel": 1,
    "jinja": True, "extra": "",
}
# 嵌入模型默认参数：CPU、512 上下文（embedding 不需要长上下文）
EMBED_DEFAULTS = {
    "kind": "embed", "host": "127.0.0.1", "port": 8081, "ctx": 512, "ngl": 0,
    "threads": 8, "kv": "", "batch": 512, "ubatch": 512, "parallel": 1,
    "jinja": False, "extra": "",
}
# 记忆**抽取器**默认参数（2026-10-15 起与聊天解耦，单独一个服务）。
#
# 为什么单独跑：抽取和聊天**共用一套权重**会把抽取带偏——微调数据里没有抽取样本
# （weiyang_train.jsonl 1085 条含抽取特征 0 条），于是微调后名字被抽坏
# （「林小雨」→「林小记得」）。实测对照（`tests/analysis/multihop_e2e.py`）：
#     抽取器            体积      真实链路多跳
#     Qwen3.5-2B       1.19GB    13/18 = 72%   ← 采用
#     官方 Qwen3-4B     2.53GB     6/9  = 67%
#     微调 v1.3（共权重）2.33GB     3/9  = 33%
# 上下文给 4096：抽取只看**一轮**（用户原话 + 未央回答），用不了大 ctx。
#
# ⚠ `ngl` 默认 **12 而不是 99**——这是实测出来的平衡点（4GB 卡，基线占 173 MiB）。
#   `tests/analysis/vram_balance_matrix.py` 的矩阵（抽取稳态耗时 / 总显存）：
#       chat 15 + ext 99 →  6.4s / 1695 MiB   ○ 抽取掉到半 CPU
#       chat 15 + ext 20 →  6.7s / 1517 MiB   ○
#       chat 15 + ext 12 →  **2.3s / 2264 MiB**  ✅ 采用（三次复测一致）
#       chat 15 + ext  0 → 10.8s / 1469 MiB   ✗ 故意纯 CPU 反而最慢
#       chat 12 + ext 99 →  1.0s / 2521 MiB   ✅ 最快，但要把 chat 从 15 降到 12
#   **反直觉但可复现**：`ngl=0`（纯 CPU）比 `ngl=12` 慢 5 倍，而中间值（20）
#   比两端都差——层在 GPU/CPU 之间来回搬的开销大于它省下的计算。
#   为什么选 12 而不是 99：chat 的 `ngl=15` 是**故意**留显存给游戏的，
#   不动它；ext 12 只比聊天单开多占 15 MiB，而抽取 2.3s 完全可接受
#   （抽取在回复之后才跑，用户无感）。
EXTRACT_DEFAULTS = {
    "kind": "chat", "host": "127.0.0.1", "port": 8083, "ctx": 4096, "ngl": 12,
    "threads": 8, "kv": "q8_0", "batch": 128, "ubatch": 64, "parallel": 1,
    "jinja": True, "extra": "",
}
# 抽取器的默认模型关键词（`ServiceManager.find_model` 用子串匹配）
EXTRACT_MODEL_KEYWORD = "Qwen3.5-2B"


class ServiceError(RuntimeError):
    """启动/停止服务时的可预期错误（带人话原因）。"""


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------- Windows Job Object（保证子进程随前台一起结束） ----------------
def _make_job_object():
    """创建一个 KILL_ON_JOB_CLOSE 的 Job Object；非 Windows 或失败时返回 None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000      # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return handle
    except Exception:
        return None


# ---------------- 模型扫描 ----------------
def discover_models(roots: list[pathlib.Path] | None = None) -> list[dict]:
    """扫描本地 GGUF 模型，按是不是嵌入模型分类。"""
    roots = roots or DEFAULT_MODEL_ROOTS
    found: list[dict] = []
    for root in roots:
        root = pathlib.Path(root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.gguf")):
            name = path.stem
            kind = "embed" if ("bge" in name.lower() or "embed" in name.lower()) else "chat"
            try:
                size_gb = path.stat().st_size / (1024 ** 3)
            except OSError:
                size_gb = 0.0
            found.append({"name": path.parent.name or name, "file": name,
                          "path": str(path), "kind": kind, "size_gb": round(size_gb, 2)})
    found.sort(key=lambda item: (item["kind"], -item["size_gb"]))
    return found


# ---------------- 端口与健康 ----------------
def port_free(host: str, port: int) -> bool:
    """端口是否可用。

    注意：不能给探测 socket 设 SO_REUSEADDR——Windows 上那样会允许绑定
    已被别人占用的端口，检测就失效了。这里先尝试连接（连得上＝有人在听），
    再尝试绑定。
    """
    host = host or "127.0.0.1"
    port = int(port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.6)
        if probe.connect_ex((host, port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def port_owner(port: int) -> int | None:
    """谁在监听这个端口（Windows 用 netstat 解析，失败返回 None）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def health(port: int, host: str = "127.0.0.1", timeout: int = 4) -> dict:
    """探测服务是否可用（/health）。"""
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")[:120]
        return {"ok": True, "detail": body}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "detail": type(exc).__name__}


# ---------------- 管理器 ----------------
class ServiceManager:
    """统一管理本机的 llama-server 进程。"""

    def __init__(self, log_dir: pathlib.Path | str = LOG_DIR,
                 model_roots: list[pathlib.Path] | None = None):
        self.log_dir = pathlib.Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.model_roots = [pathlib.Path(p) for p in (model_roots or DEFAULT_MODEL_ROOTS)]
        self.job = _make_job_object()
        self.services: dict[int, dict] = {}      # port -> handle

    # ---- 模型 ----
    def models(self) -> list[dict]:
        return discover_models(self.model_roots)

    def find_model(self, keyword: str) -> dict | None:
        for item in self.models():
            if keyword.lower() in item["path"].lower():
                return item
        return None

    # ---- 命令拼装 ----
    def build_command(self, spec: dict) -> list[str]:
        exe = shutil_which("llama-server")
        if not exe:
            raise ServiceError("PATH 里找不到 llama-server（先安装 llama.cpp）")
        model = str(spec.get("model") or "").strip()
        if not model:
            raise ServiceError("没有选模型文件")
        if not pathlib.Path(model).exists():
            raise ServiceError(f"模型文件不存在：{model}")
        kind = str(spec.get("kind") or "chat")
        cmd = [exe, "-m", model, "-c", str(int(spec.get("ctx") or 4096)),
               "--host", str(spec.get("host") or "127.0.0.1"),
               "--port", str(int(spec.get("port") or 8080)),
               "-ngl", str(int(spec.get("ngl") or 0)),
               "--parallel", str(int(spec.get("parallel") or 1)),
               "-t", str(int(spec.get("threads") or 8))]
        kv = str(spec.get("kv") or "").strip()
        if kv:
            cmd += ["--cache-type-k", kv, "--cache-type-v", kv]
        if spec.get("batch"):
            cmd += ["-b", str(int(spec["batch"]))]
        if spec.get("ubatch"):
            cmd += ["-ub", str(int(spec["ubatch"]))]
        if kind == "embed":
            cmd.append("--embeddings")
        elif spec.get("jinja", True):
            cmd.append("--jinja")
        extra = str(spec.get("extra") or "").strip()
        if extra:
            cmd += extra.split()
        return cmd

    # ---- 启动 ----
    def start(self, spec: dict, wait: bool = True, timeout: int | None = None) -> dict:
        cmd = self.build_command(spec)
        port = int(spec.get("port") or 8080)
        host = str(spec.get("host") or "127.0.0.1")
        if not port_free(host, port):
            owner = port_owner(port)
            raise ServiceError(
                f"端口 {port} 已被占用"
                + (f"（PID {owner}）" if owner else "")
                + "。先停掉它，或换个端口。")

        log_path = self.log_dir / f"{spec.get('kind', 'chat')}-{port}-{now_stamp()}.log"
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        log_file = open(log_path, "wb")
        try:
            proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=flags,
                                    cwd=str(pathlib.Path(cmd[0]).parent))
        except Exception as exc:
            log_file.close()
            raise ServiceError(f"启动失败：{exc}") from exc

        if self.job:
            try:
                import ctypes
                ctypes.WinDLL("kernel32", use_last_error=True).AssignProcessToJobObject(
                    self.job, int(proc._handle))
            except Exception:
                pass

        handle = {"port": port, "host": host, "pid": proc.pid, "proc": proc,
                  "spec": dict(spec), "cmd": cmd, "log": str(log_path),
                  "started_at": dt.datetime.now().isoformat(timespec="seconds"),
                  "log_file": log_file}
        self.services[port] = handle
        self._log_start(handle)

        if wait:
            ok, detail = self.wait_healthy(port, host, timeout or (240 if spec.get("kind") == "embed" else 300))
            handle["healthy"] = ok
            handle["detail"] = detail
            if not ok:
                self.stop(port)
                raise ServiceError(detail)
        return handle

    def wait_healthy(self, port: int, host: str = "127.0.0.1",
                     timeout: int = 300) -> tuple[bool, str]:
        """等到 /health 可用；起不来就读日志尾部给出原因。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if health(port, host)["ok"]:
                return True, "ok"
            proc = self.services.get(port, {}).get("proc")
            if proc is not None and proc.poll() is not None:
                return False, self.diagnose(port)
            time.sleep(2)
        return False, f"等待 {timeout} 秒仍未就绪。" + self.diagnose(port)

    def diagnose(self, port: int) -> str:
        """读日志尾部，把常见失败翻译成人话。"""
        handle = self.services.get(port) or {}
        log = pathlib.Path(handle.get("log", ""))
        tail = ""
        if log.exists():
            try:
                tail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
            except Exception:
                tail = ""
        low = tail.lower()
        if "address already in use" in low or "eaddr in use" in low:
            return f"端口 {port} 被占用，服务没能起来。"
        if "failed to load model" in low or "error loading model" in low or "unknown model" in low:
            return "模型加载失败（文件可能损坏或格式不对），看日志确认。"
        if "out of memory" in low or "cuda error" in low or "insufficient" in low:
            return "显存/内存不足：把 -ngl 调小（设 0 走 CPU）或换更小的量化。"
        if "no such file" in low or "cannot find" in low:
            return "找不到文件（模型路径或可执行文件不对）。"
        last = " / ".join(line.strip() for line in tail.strip().splitlines()[-3:])
        return f"服务未能就绪。日志尾部：{last[:300]}" if last else "服务未能就绪（日志为空）。"

    def _log_start(self, handle: dict) -> None:
        """启动日志：时间、模型、参数、PID（模块四要求）。"""
        spec = handle["spec"]
        record = {"time": handle["started_at"], "kind": spec.get("kind", "chat"),
                  "model": spec.get("model"), "port": handle["port"],
                  "pid": handle["pid"], "cmd": " ".join(handle["cmd"]),
                  "params": {k: v for k, v in spec.items() if k not in ("model",)}}
        try:
            with open(START_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---- 停止 ----
    def stop(self, port: int, graceful: bool = True) -> bool:
        handle = self.services.pop(port, None)
        if not handle:
            owner = port_owner(port)
            if owner:
                self._kill_pid(owner)
                return True
            return False
        proc = handle.get("proc")
        try:
            if handle.get("log_file"):
                handle["log_file"].close()
        except Exception:
            pass
        if proc is None:
            return False
        try:
            if proc.poll() is None:
                if graceful:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        self._kill_pid(proc.pid)
                else:
                    self._kill_pid(proc.pid)
        except Exception:
            self._kill_pid(getattr(proc, "pid", 0))
        return True

    def _kill_pid(self, pid: int) -> None:
        if not pid:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            else:
                os.kill(pid, 9)
        except Exception:
            pass

    def stop_all(self) -> list[int]:
        """停掉本管理器起过的所有服务（前台退出时调用）。"""
        stopped = []
        for port in list(self.services):
            if self.stop(port):
                stopped.append(port)
        return stopped

    # ---- 状态 ----
    def port_free(self, host: str, port: int) -> bool:
        return port_free(host, port)

    def port_owner(self, port: int) -> int | None:
        return port_owner(port)

    def health(self, port: int, host: str = "127.0.0.1") -> dict:
        return health(port, host)

    def status(self, ports: list[int] | None = None) -> list[dict]:
        ports = ports or [8080, 8081]
        out = []
        for port in ports:
            handle = self.services.get(port)
            owner = port_owner(port)
            ok = health(port)["ok"] if owner else False
            out.append({
                "port": port,
                "listening": bool(owner),
                "pid": owner,
                "healthy": ok,
                "managed": bool(handle),
                "model": (handle or {}).get("spec", {}).get("model", ""),
            })
        return out

    def tail_log(self, port: int, lines: int = 40) -> str:
        handle = self.services.get(port)
        if not handle:
            return "（这个端口不是本程序启动的，没有日志）"
        path = pathlib.Path(handle["log"])
        if not path.exists():
            return "（还没有日志）"
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)
