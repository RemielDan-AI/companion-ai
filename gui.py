#!/usr/bin/env python
"""未央 · 桌面窗口（Tkinter）。

一个能直接聊的应用窗口：
- 对话走完整流程（技能路由 → 自动选档 → 记忆检索 → 生成 → 后处理 → 记忆/价值观）
- 语音可切换：不语音 / Edge TTS（选声音）/ GSV TTS Lite（选语言）/ OpenAI 兼容
- 底部按钮：好、不好、纠错（会写成 DPO 偏好对）、记忆、技能、价值观

用法：python gui.py     （或双击 启动界面.bat）
"""
from __future__ import annotations

import json
import pathlib
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import Companion, LEVEL_NAMES, format_memory_state  # noqa: E402
from cloud import gateway  # noqa: E402
from cognition import ai_profile as ai_profile_engine  # noqa: E402
from cognition import values as values_engine  # noqa: E402
from engine import translate as translate_engine  # noqa: E402
from feedback import collector  # noqa: E402
from services.manager import ServiceError, ServiceManager  # noqa: E402
from services.presets import PresetStore  # noqa: E402
from tools import executor, mcp_client  # noqa: E402
from voice import tts as tts_engine  # noqa: E402

FONT_UI = ("微软雅黑", 10)
FONT_CHAT = ("微软雅黑", 11)
FONT_TITLE = ("微软雅黑", 13, "bold")
FONT_META = ("微软雅黑", 9)


def style_dialog(win: tk.Misc) -> None:
    """给弹窗 / 对话框铺主界面同款深色底（背景风格统一）。

    为什么要单独做：`_setup_look()` 只给主窗 root 设了 bg 和 ttk 样式，
    Toplevel 自己没人设 bg → 露出 Tk 默认浅灰，于是模型服务、记忆管理等
    弹窗四周和控件缝隙就成了"背景覆盖不全"的样子。

    ttk 样式是解释器级共享的，主窗配过就一起生效；这里只补 Toplevel 自身的底色。
    取不到主题模块时不硬套颜色，交给调用方。
    """
    try:
        from gui_theme import COLORS as _colors
    except Exception:
        return                      # 拿不到主题就保持系统默认色，不硬套
    bg = _colors.get("bg")
    if not bg:
        return
    try:
        win.configure(bg=bg)
    except Exception:
        pass


def _greet() -> str:
    """开场白（内容见 `config/replies.yaml` 的 `greeting`）。"""
    try:
        from engine import replies
        return replies.get("greeting", "嗯，我在呢。今天想聊点什么？")
    except Exception:
        return "嗯，我在呢。今天想聊点什么？"


def _greet_ja() -> str:
    """日文开场白（`greeting_ja`）。"""
    try:
        from engine import replies
        return replies.get("greeting_ja", "嗯，我在呢。こんにちは、私は未央です。")
    except Exception:
        return "嗯，我在呢。こんにちは、私は未央です。"


def theme_scroll(widget: tk.Misc, colors: dict) -> None:
    """给 ScrolledText 内建的 Frame + Scrollbar 染色。

    这两个子控件默认取系统色（SystemButtonFace），在深色弹窗里会露出浅灰边框和
    浅色滚动槽 —— 就是"背景覆盖不全"的来源之一。

    坑（实测）：ScrolledText 继承自 Text，它的 winfo_children() **返回空**，
    内建 Frame 挂在父控件那条链上；所以不能靠遍历子控件找，要用它自己的
    .frame / .vbar 属性。传进来的也可以是普通 widget，此时递归找一遍。
    """
    bg = colors.get("bg", "#0d1220")
    trough = colors.get("panel", "#161A2E")

    def paint(frame, bar=None) -> None:
        if frame is not None:
            try:
                frame.configure(bg=bg)
            except Exception:
                pass
        if bar is not None:
            try:
                bar.configure(bg=trough, troughcolor=bg,
                              activebackground=colors.get("accent", "#4D6BFE"),
                              borderwidth=0, highlightthickness=0)
            except Exception:
                pass

    inner_frame = getattr(widget, "frame", None)
    inner_bar = getattr(widget, "vbar", None)
    if inner_frame is not None or inner_bar is not None:
        try:
            widget.update_idletasks()
        except Exception:
            pass
        paint(inner_frame, inner_bar)
        return

    # 普通 widget：递归找 ScrolledText / Scrollbar
    def walk(parent: tk.Misc) -> None:
        for child in parent.winfo_children():
            kw = getattr(child, "vbar", None)
            kf = getattr(child, "frame", None)
            if kw is not None or kf is not None:
                paint(kf, kw)
            elif child.winfo_class() == "Scrollbar":
                paint(None, child)
            walk(child)

    walk(widget)


def center_window(win: tk.Misc, width: int | None = None,
                  height: int | None = None) -> None:
    """把 Toplevel 摆到**屏幕正中**（不依赖窗口管理器给的位置）。

    这些弹窗原先只调用 geometry("980x560")：只给尺寸不给坐标时，
    位置由 WM 决定，实测会跑到屏幕偏上/偏左的位置，看着像"没居中"。
    这里按实际请求尺寸算一次居中坐标；窗口比屏幕大时贴住左上角，不出现负坐标。
    """
    try:
        win.update_idletasks()
        w = width or win.winfo_width() or win.winfo_reqwidth()
        h = height or win.winfo_height() or win.winfo_reqheight()
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        x = max(0, (screen_w - w) // 2)
        y = max(0, (screen_h - h) // 2)
        win.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        pass


class ScrollableRow(ttk.Frame):
    """一行「装不下就横向滚动」的控件容器（功能按钮排专用）。

    原来这排直接用 Frame：按钮总宽超过窗口宽度时，Tk 会把**最后一个按钮压扁**，
    字号一调大就整排挤没。放进 Canvas 后，内层 frame 保持自然宽度，
    窗口不够就横向滚动，按钮不会被压掉。
    把子控件放进 `.inner` 即可。
    """

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        bg = "#0d1220"
        try:
            from gui_theme import COLORS as _colors
            bg = _colors.get("bg", bg)
        except Exception:
            pass
        self.canvas = tk.Canvas(self, height=34, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="x", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner,
                                                 anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        for widget in (self.canvas, self.inner):
            widget.bind("<MouseWheel>", self._on_wheel)

    def _on_inner_configure(self, _event=None) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_configure(self, event) -> None:
        # 内层至少铺满可视区；真正超宽时保持自然宽度（触发横向滚动）
        try:
            need = self.inner.winfo_reqwidth()
            self.canvas.itemconfigure(self._window,
                                      width=max(event.width, need))
        except Exception:
            pass

    def _on_wheel(self, event) -> None:
        try:
            self.canvas.xview_scroll(-1 if getattr(event, "delta", 0) > 0 else 1,
                                     "units")
        except Exception:
            pass

    def bind_wheel_tree(self) -> None:
        """把滚轮事件也绑到内层每个子控件上（鼠标停在按钮上时同样能滚）。"""

        def walk(widget) -> None:
            widget.bind("<MouseWheel>", self._on_wheel)
            for child in widget.winfo_children():
                walk(child)

        walk(self.inner)

BACKENDS = ["不语音", "Edge TTS", "GSV TTS Lite", "OpenAI 兼容"]
BACKEND_KEYS = {"不语音": None, "Edge TTS": "edge",
                "GSV TTS Lite": "gsv", "OpenAI 兼容": "openai"}
# 这个版本 GSV-TTS-Lite 的语言前端只支持 auto / zh / ja / en
# （gsv_tts/TTS.py 里 text_language 的 Literal 就这四个），所以朗读语言只给这几种。
# 试听用的句子：必须用该语言的文本，否则听不出语言差异
SAMPLE_TEXT = {
    "auto": _greet_ja(),
    "zh": _greet(),
    "ja": "こんにちは、私は未央です。今日は何を話しましょうか。",
    "en": "Hi, I'm Weiyang. What shall we talk about?",
}

# 「文本语言」：未央用什么语言写回复
TEXT_LANGS = {"中文": "zh", "日本語": "ja", "English": "en"}
# 「朗读语言」：语音念成什么语言；与文本语言相互独立，"自动"= 跟随文本语言
VOICE_LANGS = {"自动": None, "中文": "zh", "日本語": "ja", "English": "en"}
LANG_PROMPTS = {
    "zh": "",
    "ja": ("这次请用日语回复。要自然的日常口语，像日本人平时说话那样，"
           "不要翻译腔，不要加罗马字注音，也不要中英混着写。"),
    "en": "Reply in English this time — natural, casual, like talking to someone close.",
}
EDGE_PREFIX = {"zh": "zh-", "ja": "ja-", "en": "en-"}
KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")

# 校验没过时的加强版指令（4B 模型对埋在中间的指令遵从率不够，这条放最末尾）
LANG_RETRY = {
    "ja": "【必须遵守】这次请完全用日语回答：写日语假名和日语表达，不要写成中文句子。",
    "en": "【Required】Answer in English only this time. Do not write Chinese.",
}


def lang_matches(text: str, lang: str) -> bool:
    """判断回复是否真的用了目标语言。"""
    if lang == "ja":
        return bool(KANA_RE.search(text or ""))
    if lang == "en":
        letters = sum(1 for ch in (text or "") if ch.isascii() and ch.isalpha())
        return letters >= max(6, len(text or "") * 0.4)
    return True


class Window:
    def __init__(self, root: tk.Tk, companion: Companion | None = None):
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self.last_audio = ""
        self.ui_state = tts_engine.load_ui_state()
        self.voice_config = tts_engine.load_config()
        # 对话框显示项（可在设置窗口里改，这里按保存过的偏好初始化）
        self.show_meta = str(self.ui_state.get("show_meta", "显示")) != "隐藏"
        self.show_replay = str(self.ui_state.get("show_replay", "显示")) != "隐藏"
        try:
            if self.ui_state.get("inject_memory") == "关闭":
                self.inject_memory = False
        except Exception:
            pass

        self.companion = companion or Companion()
        self.companion.tts_enabled = False      # 语音由窗口自己控制
        self.replies: list[str] = []            # AI 回复历史，供「回听」用
        self.speech_cache: dict[tuple[str, str], str] = {}   # 朗读译文缓存
        # 朗读通道状态：idle / working（正在合成）/ playing（正在播）
        self._speech_state = "idle"
        self._speech_token = 0
        self._play_started = 0.0
        self.max_play_seconds = float(self.voice_config.get("max_play_seconds", 120))
        # 等待语音就绪的回复：合成期间只显示占位，语音好了文本和声音一起出现
        self.pending_reply: dict | None = None
        self._pending_line: int | None = None
        # 模块四：统一服务管理（模型/参数/预设/无窗口启动/退出清理）
        self.services = ServiceManager()
        self.presets = PresetStore()
        self.svc_win: tk.Toplevel | None = None
        self.svc_vars: dict = {}
        self.stop_services_on_exit = bool(self.ui_state.get("stop_services_on_exit", True))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._restore_history()
        self.root.after(120, self._poll)
        self._check_services()

    def _restore_history(self) -> None:
        """启动时把最近几轮对话恢复进窗口，保证跨会话连续（项目改进v1 基础改进 1）。"""
        try:
            episodes = list(reversed(self.companion.memory.recent_episodes(limit=6)))
        except Exception:
            episodes = []
        if not episodes:
            self._append("未央", _greet(), "ai")
            return
        when = (episodes[-1].get("created_at") or "")[:16].replace("T", " ")
        self._append("", f"—— 上次聊到 {when}，下面是之前说过的 ——\n", "meta")
        for item in episodes:
            self._append("你", item.get("user_text") or "", "user")
            self._append("未央", item.get("reply_text") or "", "ai")
            self.replies.append(item.get("reply_text") or "")
        self.status_var.set(f"已恢复最近 {len(episodes)} 轮对话（长期记忆照常生效）")

    # ---------------- 界面 ----------------
    def _setup_look(self) -> None:
        """外观第 1 步：深色鲸蓝主题 + 顶部状态区（模型/记忆/服务）。

        只动外观，不改任何功能逻辑；任何一步失败都直接跳过，
        保证界面照旧能用（宁可难看，也不能打不开）。
        """
        try:
            from gui_theme import COLORS, StatusBar, pick_font
        except Exception as exc:
            print(f"（外观模块不可用，跳过美化：{exc}）")
            self.status = None
            return
        try:
            pick_font(self.root)
            self.root.configure(bg=COLORS["bg"])
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            style.configure("TFrame", background=COLORS["panel"])
            style.configure("TLabel", background=COLORS["panel"],
                            foreground=COLORS["text"])
            style.configure("TLabelframe", background=COLORS["panel"],
                            foreground=COLORS["text"])
            style.configure("TLabelframe.Label", background=COLORS["panel"],
                            foreground=COLORS["text_dim"])
            style.configure("TButton", background=COLORS["panel_hi"],
                            foreground=COLORS["text"], borderwidth=0, focusthickness=0)
            style.map("TButton", background=[("active", COLORS["accent"])])
            style.configure("TCheckbutton", background=COLORS["panel"],
                            foreground=COLORS["text"])
            # Treeview：记忆管理 / 工具调用两个弹窗用它。不配的话是系统白底，
            # 在深色弹窗里就是一大块白 —— 弹窗背景"覆盖不全"的另一半原因。
            style.configure("Treeview",
                            background=COLORS["bg"], fieldbackground=COLORS["bg"],
                            foreground=COLORS["text"], borderwidth=0, relief="flat",
                            rowheight=24)
            style.map("Treeview",
                      background=[("selected", COLORS["accent"])],
                      foreground=[("selected", "#FFFFFF")])
            style.configure("Treeview.Heading",
                            background=COLORS["panel_hi"], foreground=COLORS["text"],
                            borderwidth=0, relief="flat")
            style.map("Treeview.Heading",
                      background=[("active", COLORS["accent"])])
            style.configure("TSpinbox", fieldbackground=COLORS["panel_hi"],
                            background=COLORS["panel_hi"], foreground=COLORS["text"],
                            arrowcolor=COLORS["text"], borderwidth=0)
            style.configure("TCombobox", fieldbackground=COLORS["panel_hi"],
                            background=COLORS["panel_hi"], foreground=COLORS["text"],
                            arrowcolor=COLORS["text"], borderwidth=0)
            # 只读下拉框的文字颜色要靠 map 单独设，否则白底浅字＝看不见选了什么
            style.map("TCombobox",
                      fieldbackground=[("readonly", COLORS["panel_hi"])],
                      foreground=[("readonly", COLORS["text"])],
                      background=[("readonly", COLORS["panel_hi"])],
                      selectbackground=[("readonly", COLORS["accent"])],
                      selectforeground=[("readonly", COLORS["text"])])
            style.configure("TEntry", fieldbackground=COLORS["panel_hi"],
                            foreground=COLORS["text"], insertcolor=COLORS["text"],
                            borderwidth=0)
            style.map("TEntry",
                      fieldbackground=[("disabled", COLORS["panel"]),
                                       ("!disabled", COLORS["panel_hi"])],
                      foreground=[("disabled", COLORS["text_dim"]),
                                  ("!disabled", COLORS["text"])])
            # 下拉展开的那一列是原生 Listbox，只能通过 option 数据库染色
            for pattern, value in (
                    ("*TCombobox*Listbox.background", COLORS["panel_hi"]),
                    ("*TCombobox*Listbox.foreground", COLORS["text"]),
                    ("*TCombobox*Listbox.selectBackground", COLORS["accent"]),
                    ("*TCombobox*Listbox.selectForeground", COLORS["text"])):
                self.root.option_add(pattern, value)
            self.status = StatusBar(self.root)
            self.status.pack(fill="x", side="top")
            self._refresh_status()
            self.root.after(15000, lambda: self._status_tick())
        except Exception as exc:
            print(f"（外观初始化失败，保持原样：{exc}）")
            self.status = None

    def _status_tick(self) -> None:
        self._refresh_status()
        try:
            self.root.after(15000, lambda: self._status_tick())
        except Exception:
            pass

    def _refresh_status(self) -> None:
        """刷新顶部状态区：模型名 / 记忆条数 / 两个服务是否健康。"""
        status = getattr(self, "status", None)
        if status is None:
            return

        def work() -> None:
            model_text, model_level = "模型：—", "dim"
            try:
                import urllib.request
                base = str(getattr(self.companion.generator, "api_url", "")).split("/v1/")[0]
                if base:
                    with urllib.request.urlopen(base + "/v1/models", timeout=3) as resp:
                        data = json.loads(resp.read().decode("utf-8", "replace"))
                    items = data.get("data") or data.get("models") or []
                    if items:
                        name = str(items[0].get("id") or items[0].get("name") or "")
                        name = name.replace("\\", "/").split("/")[-1]
                        model_text, model_level = f"模型：{name}", "ok"
            except Exception:
                model_text, model_level = "模型：未连接", "warn"

            try:
                semantics = self.companion.memory.semantic_count()
                episodes = self.companion.episode_count()
                memory_text = f"记忆：{semantics} 语义 / {episodes} 情景"
                memory_level = "dim"
            except Exception:
                memory_text, memory_level = "记忆：—", "dim"

            service_text, service_level = "服务：—", "dim"
            try:
                manager = ServiceManager()
                chat = manager.health(8080).get("ok")
                embed = manager.health(8081).get("ok")
                if chat and embed:
                    service_text, service_level = "服务：对话 ✓ 嵌入 ✓", "ok"
                elif chat:
                    service_text, service_level = "服务：对话 ✓ 嵌入 ✗", "warn"
                else:
                    service_text, service_level = "服务：对话 ✗ 嵌入 ✗", "err"
            except Exception:
                service_text, service_level = "服务：—", "dim"

            def apply() -> None:
                status.set("model", model_text, model_level)
                status.set("memory", memory_text, memory_level)
                status.set("service", service_text, service_level)

            try:
                self.root.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _build(self) -> None:
        self.root.title("未央 · 陪伴 AI")
        self.root.geometry("1000x730")
        self.root.minsize(900, 700)
        self._setup_look()

        # 配色**一次性取好**给整个 `_build` 用。
        # ⚠ 必须放在函数开头：底部控件先于左栏创建，晚取就会 UnboundLocalError。
        try:
            from gui_theme import COLORS as _LC
        except Exception:
            _LC = {}

        # 顶部：只留一个「⚙ 设置」入口——具体设置在独立窗口里（参考 Cyrene 的分栏设置）
        # 这一排控件**继续创建但不显示**：它们的变量与事件绑定都还在，所以功能不受影响。
        # ⚠ 2026-10-24：原来这里有一条自建"品牌条"（未央 · 陪伴 AI + ⚙设置），
        #   但 `_setup_look` 里的 `StatusBar` 已经在最上面了，**两条重复**，
        #   而且白占一行高度（左栏功能按钮被挤出去了）。
        #   现在把「设置」并进左栏功能网格，顶部只留 StatusBar。
        self.header_hint = None       # 兼容旧引用（别处只是给它 settext）

        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        # top.pack(fill="x")   # 设置已移到独立窗口，这里不再显示
        ttk.Label(top, text="语音", font=FONT_UI).pack(side="left")
        self.backend_var = tk.StringVar(value=self.ui_state.get("backend", "不语音"))
        self.backend_box = ttk.Combobox(top, textvariable=self.backend_var, values=BACKENDS,
                                        width=13, state="readonly", font=FONT_UI)
        self.backend_box.pack(side="left", padx=(6, 10))
        self.backend_box.bind("<<ComboboxSelected>>", self._on_backend_change)

        # Edge 声音
        self.edge_voice_var = tk.StringVar(
            value=self.ui_state.get("edge_voice",
                                    (self.voice_config.get("edge") or {}).get(
                                        "voice", "zh-CN-XiaoxiaoNeural")))
        ttk.Label(top, text="声音", font=FONT_UI).pack(side="left")
        self.edge_box = ttk.Combobox(top, textvariable=self.edge_voice_var,
                                     width=30, font=FONT_UI)
        self.edge_box.pack(side="left", padx=(6, 10))

        # 文本语言：未央用什么语言"写"
        self.text_lang_var = tk.StringVar(
            value=self.ui_state.get("text_lang", self.ui_state.get("reply_lang", "中文")))
        if self.text_lang_var.get() not in TEXT_LANGS:
            self.text_lang_var.set("中文")
        ttk.Label(top, text="文本语言", font=FONT_UI).pack(side="left")
        self.text_lang_box = ttk.Combobox(top, textvariable=self.text_lang_var,
                                          values=list(TEXT_LANGS), width=8,
                                          state="readonly", font=FONT_UI)
        self.text_lang_box.pack(side="left", padx=(6, 10))
        self.text_lang_box.bind("<<ComboboxSelected>>", self._on_text_lang_change)

        # 朗读语言：语音"念成"什么语言（与文本语言独立）
        saved_voice_lang = self.ui_state.get("voice_lang")
        if saved_voice_lang is None:      # 兼容旧状态文件里的 gsv_lang
            old = self.ui_state.get("gsv_lang", "auto")
            saved_voice_lang = {"auto": "自动", "zh": "中文", "ja": "日本語",
                                "en": "English"}.get(old, "自动")
        if saved_voice_lang not in VOICE_LANGS:
            saved_voice_lang = "自动"
        self.voice_lang_var = tk.StringVar(value=saved_voice_lang)
        ttk.Label(top, text="朗读语言", font=FONT_UI).pack(side="left")
        self.voice_lang_box = ttk.Combobox(top, textvariable=self.voice_lang_var,
                                           values=list(VOICE_LANGS), width=8,
                                           state="readonly", font=FONT_UI)
        self.voice_lang_box.pack(side="left", padx=(6, 10))
        self.voice_lang_box.bind("<<ComboboxSelected>>", self._on_voice_lang_change)

        self.autoplay_var = tk.BooleanVar(value=bool(self.ui_state.get("autoplay", True)))
        ttk.Checkbutton(top, text="自动朗读", variable=self.autoplay_var,
                        command=self._save_state).pack(side="left", padx=(0, 10))
        ttk.Button(top, text="试听", command=self._try_voice).pack(side="left")
        self.replay_button = ttk.Button(top, text="🔊 重听上一句", command=self._replay_last)
        self.replay_button.pack(side="left", padx=6)
        ttk.Button(top, text="刷新声音", command=self._refresh_voices).pack(side="left")

        # 底部两排 + 状态行：**必须**在 middle（expand）之前 pack。
        # Tk 的 pack 是按调用顺序从剩余空腔里逐块切空间的：聊天区一旦
        # 带 expand 先入场，就会把竖直空间吃光，轮到底部时腔体高度为 0，
        # 输入框与功能按钮整排被丢弃（实测 mapped=0 / 1x1）。
        # 输入区：仍然先于聊天区创建（Tk 的 pack 是按调用顺序切空腔的，
        # 聊天区一带 expand 先入场会把竖直空间吃光，底部整排被丢弃——踩过）。
        #
        # ⚠ 2026-10-27 重做：用户反馈"输入栏太细、难点"。
        #   原来是一个 `ttk.Entry`（高 ~26px，无描边，贴在窗口最底部），
        #   在深色界面上几乎看不出来是个输入框。现在改成：
        #     · **外层描边**（外框用强调深色，内框用面板色 → 视觉上是一块框）
        #     · **两行高**的 `tk.Text`（能一眼认出是输入区，也放得下长句子）
        #     · **发送按钮加宽加高**，用强调色底
        #     · 底下加一行小字提示「Enter 发送 · Shift+Enter 换行」
        _border = _LC.get("accent_deep", "#2B3A8F")
        _panel_bg = _LC.get("panel", "#161A2E")
        _text_fg = _LC.get("text", "#E8ECFF")

        bottom = tk.Frame(self.root, bg=_LC.get("bg", "#0F1220"))
        bottom.pack(side="bottom", fill="x", padx=10, pady=(6, 4))

        # ⚠ 提示行**必须先 pack**：Tk 的 pack 是按调用顺序切空腔的，
        #   输入框带 `expand=True` 一旦先入场就会把整块空间用光，
        #   后 pack 的提示行分不到高度、直接消失（和左栏功能按钮那次是同一个坑）。
        tk.Label(bottom, text="Enter 发送 · Shift+Enter 换行",
                 bg=_LC.get("bg", "#0F1220"),
                 fg=_LC.get("text_dim", "#9AA6CC"),
                 font=FONT_META).pack(side="bottom", anchor="w", padx=2)

        box = tk.Frame(bottom, bg=_border)            # 描边（靠 1px 边距露出来）
        box.pack(side="left", fill="x", expand=True)
        inner = tk.Frame(box, bg=_panel_bg)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        # 两行高的输入框（Text 比 Entry 更像"输入区"，也更好点）
        self.entry = tk.Text(inner, height=2, wrap="word",
                             font=FONT_CHAT, relief="flat", bd=0,
                             bg=_panel_bg, fg=_text_fg,
                             insertbackground=_text_fg,
                             padx=10, pady=8, highlightthickness=0)
        self.entry.pack(fill="both", expand=True)
        # Enter 发送、Shift+Enter 换行（Text 默认回车是换行，必须显式处理）
        self.entry.bind("<Return>", self._on_send)
        self.entry.bind("<Shift-Return>", lambda _e: None)   # 放行换行

        self.send_button = ttk.Button(bottom, text="发送", width=8,
                                      command=self._on_send)
        self.send_button.pack(side="left", fill="y", padx=(8, 0))
        try:
            ttk.Style().configure("TButton", padding=(6, 10))
        except Exception:
            pass

        # ⚠ 2026-10-24：这排功能按钮从**底部**搬到了**左栏**。
        #   原因：底部横向一长排会把输入框挤窄，而且九个按钮连成一排很难扫。
        #   现在左栏竖着排成两列网格，底部只留输入框和发送键，视线更干净。
        #   （`ScrollableRow` 仍保留给别处用，这里不再需要。）

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, font=FONT_META,
                  foreground=_LC.get("text_dim", "#9AA6CC"),
                  background=_LC.get("bg", "#0F1220"),
                  anchor="w", padding=(20, 0, 12, 6)).pack(side="bottom", fill="x")

        # 中部：对话记录
        # 左侧：角色区（有立绘就用立绘，否则自绘鲸鱼娘）——取不到就跳过，不影响聊天
        # 反馈按钮（好/不好/纠错）放在立绘下面：每次回复后都要点，放左边更顺手，
        # 也不占底部输入区的横向空间（底部按钮太多会把输入框挤出去）。
        # ---- 左栏：形象是主角 ----
        # 布局：形象卡（深色面板）→ 好/不好/纠错（每轮都要点，放顺手位置）
        #       → 功能入口（两列网格）
        _panel = _LC.get("panel", "#161A2E")
        _bg = _LC.get("bg", "#0F1220")
        _dim = _LC.get("text_dim", "#9AA6CC")

        left_col = tk.Frame(self.root, bg=_bg, width=286)
        left_col.pack(side="left", fill="y", padx=(10, 0), pady=(8, 6))
        left_col.pack_propagate(False)
        # ⚠ `pack_propagate(False)` 之后子控件溢出会被**直接裁掉**（不是压扁）。
        #   所以窗口太矮时，功能网格会无声消失——把最小窗口高度调大，
        #   并在 `minsize` 里保证够放下"形象 268 + 反馈 + 5 行按钮"。

        # ⚠ **打包顺序即优先级**：先把"反馈 + 功能网格"用 `side="bottom"` 占住底部，
        #   形象卡再用 `expand=True` 吃剩下的空间。
        #   这样窗口矮的时候被牺牲的是**形象**（装饰），而不是按钮（功能）——
        #   反过来的写法会让最后几行按钮被 `pack_propagate(False)` **直接裁掉**
        #   （实测：720 高的窗口里「模型服务」「AI 档案」等四五个按钮凭空消失）。
        # ⚠⚠ **Tk 的 pack 是按调用顺序切空腔的，不是按 side 优先级**。
        #   所以「底部那坨」必须**先 pack**（side="bottom" 先占住底部），
        #   形象卡再进场吃剩下的；反过来的话 `expand=True` 的形象卡会把空腔吃光，
        #   后进场的底部块**分不到任何高度**，按钮直接消失（踩了两次）。
        _bottom_first = tk.Frame(left_col, bg=_bg)
        _bottom_first.pack(side="bottom", fill="x")

        # 形象卡：把图放在一块独立面板里，和聊天区分开
        art_card = tk.Frame(left_col, bg=_panel)
        art_card.pack(side="top", fill="both", expand=True)
        try:
            from gui_theme import CharacterPanel
            _cw, _ch = 236, 230
            try:
                import yaml as _yaml
                _g = _yaml.safe_load((pathlib.Path(__file__).resolve().parent
                                      / "config" / "gui.yaml")
                                     .read_text(encoding="utf-8")) or {}
                _cw = int(_g.get("character_width") or _cw)
                _ch = int(_g.get("character_height") or _ch)
            except Exception:
                pass
            self.character = CharacterPanel(art_card, width=_cw, height=_ch)
            self.character.pack(fill="x")
            self.character.set_state("在听你说")
        except Exception as exc:
            self.character = None
            print(f"（角色区跳过：{exc}）")

        # 反馈：三颗按钮横排（竖排太占高度，横排一眼能扫完）
        feedback = tk.Frame(_bottom_first, bg=_bg)
        feedback.pack(fill="x", pady=(8, 6))
        for text, cmd in (("👍 好", lambda: self._feedback("up")),
                          ("👎", lambda: self._feedback("down")),
                          ("✏ 纠错", self._correct)):
            ttk.Button(feedback, text=text, width=7, command=cmd).pack(
                side="left", expand=True, fill="x", padx=2)

        tk.Frame(_bottom_first, bg=_LC.get("panel_hi", "#1F2544"),
                 height=1).pack(fill="x", pady=(0, 6))
        tk.Label(_bottom_first, text="功能", bg=_bg, fg=_dim,
                 font=FONT_META).pack(anchor="w", padx=2)
        # 功能入口：两列网格（九个按钮 → 5 行，比底部横排好扫）
        # ⚠ **两列**：三列时每个按钮只有 ~88px，五字标签会被截成
        #   「清空上下」「记忆管」「AI 档」——**截断的按钮等于看不懂**。
        #   两列每个 ~140px，全部标签完整。
        #   高度够不够不是靠猜：`_bottom_first` 是**先 pack** 的（先占住底部），
        #   所以按钮永远拿得到自己需要的高度，被压缩的是上面的形象区。
        #   `tests/layout_test.py` 会实测"每个按钮都完整落在窗口内"。
        grid = tk.Frame(_bottom_first, bg=_bg)
        grid.pack(fill="x", pady=(4, 0))
        for _c in range(2):
            grid.columnconfigure(_c, weight=1, uniform="fn")
        _actions = [
            ("⚙ 设置", self._open_settings),
            ("记忆", lambda: self._show_text("记忆现状",
                                             format_memory_state(self.companion))),
            ("技能", lambda: self._show_text("技能清单",
                                             self.companion.router.table())),
            ("价值观", lambda: self._show_text("价值观库",
                                               values_engine.table(self.companion.store))),
            ("清空上下文", self._reset),
            (("🧹 清除记忆" if True else ""), self._wipe_dialog),
            ("📚 记忆管理", self._memory_manager),
            ("⚙ 模型服务", self._service_window),
            ("🎭 AI 档案", self._ai_profile_window),
            ("🔧 工具调用", self._tool_calls_window),
        ]
        for idx, (text, cmd) in enumerate(_actions):
            btn = ttk.Button(grid, text=text, command=cmd)
            btn.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=2, pady=2)
            if text.endswith("清除记忆"):
                self.wipe_button = btn
        middle = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        middle.pack(side="left", fill="both", expand=True)
        # 配色取自 gui_theme（深色鲸蓝）；取不到就用原来的浅色兜底
        try:
            from gui_theme import COLORS as _C
        except Exception:
            _C = {}
        self.chat = scrolledtext.ScrolledText(
            middle, wrap="word", font=FONT_CHAT, relief="flat",
            background=_C.get("bg", "#fbfbfd"),
            foreground=_C.get("text", "#1b1b1b"),
            insertbackground=_C.get("text", "#1b1b1b"),
            padx=14, pady=12, borderwidth=0, highlightthickness=0)
        self.chat.pack(fill="both", expand=True)
        # 保持 normal 状态才能点「🔊 回听」；用键盘拦截做到只读（可选中、可复制）
        self.chat.bind("<Key>", self._block_typing)
        # 气泡观感第一版：用整行背景色区分"对方/AI"，右对齐＋留白，像聊天软件那样分块；
        # 真正的左右分离圆角气泡需要替换掉 Text 控件（见 gui_theme.Bubble），另行评估。
        self.chat.tag_config("user", foreground=_C.get("text", "#1a5fb4"),
                             background=_C.get("bubble_me", "#e8f0fe"),
                             spacing1=8, spacing3=8, lmargin1=48, lmargin2=48,
                             justify="right")
        self.chat.tag_config("ai", foreground=_C.get("text", "#1b1b1b"),
                             background=_C.get("bubble_ai", "#f2f3f7"),
                             spacing1=8, spacing3=8, lmargin1=8, lmargin2=8)
        self.chat.tag_config("meta", foreground=_C.get("text_dim", "#8a8a8a"),
                             font=FONT_META, spacing3=10, lmargin1=12, lmargin2=12)
        self.chat.tag_config("error", foreground=_C.get("err", "#c01c28"), spacing3=8,
                             lmargin1=12)
        self.chat.tag_config("pending", foreground=_C.get("text_dim", "#8a8a8a"),
                             background=_C.get("bubble_ai", "#f2f3f7"),
                             spacing1=6, spacing3=6, lmargin1=8, lmargin2=8)
        # 鼠标移到「回听」上：字色变主题深蓝，**1 秒后自动变回**（底色全程不动）
        self.chat.tag_config("replay",
                             foreground=_C.get("text", "#E8ECFF"),
                             background=_C.get("bubble_ai", "#1A1F38"),
                             font=FONT_META, spacing1=8, spacing3=8,
                             lmargin1=8, lmargin2=8)
        # 鼠标移到「回听」上：字色变主题深蓝，**1 秒后自动变回**（底色全程不动）
        self._replay_hover_fg = _C.get("accent_deep", "#2B3A8F")
        self._replay_timer: str | None = None      # 唯一的高亮复原计时器
        # 播放联动用：正在朗读的那条链接 + 用来"隐形"的字色（=气泡底色）
        self._replay_blank_fg = _C.get("bubble_ai", "#1A1F38")
        self._active_replay_link: str | None = None

        # ---------------- 让对话可以用鼠标选中、复制 ----------------
        # ⚠ **Tk 的经典陷阱**（2026-10-19 用户反馈"选不中"）：
        #   这个 Text 的每一行都挂了带 `background` 的 tag（`user`/`ai` 的气泡底色）。
        #   **Tk 里后创建的 tag 优先级更高**，而内建的 `sel`（选区高亮）是**最早创建**的，
        #   于是选区被气泡底色盖住 → **选中了也看不见**，用起来就像"没法选"。
        #   修法两步：
        #     ① `selectbackground/selectforeground` 明确设成跟所有气泡色都不同的颜色
        #     ② `tag_raise("sel")` 把选区提到**最高优先级**（必须排在自定义 tag 之后调用）
        self.chat.configure(
            selectbackground=_C.get("select_bg", "#2f6fed"),
            selectforeground=_C.get("select_fg", "#ffffff"),
            inactiveselectbackground=_C.get("select_bg", "#2f6fed"),
            # 允许把选中内容同步到系统剪贴板（Tk 默认就是 1，显式写出来避免被别处改掉）
            exportselection=True,
        )
        try:
            self.chat.tag_raise("sel")
        except Exception:
            pass
        # 右键菜单：键盘快捷键之外的兜底（有的人习惯右键复制）
        self._chat_menu = tk.Menu(self.chat, tearoff=0)
        self._chat_menu.add_command(label="复制", accelerator="Ctrl+C",
                                    command=lambda: self.chat.event_generate("<<Copy>>"))
        self._chat_menu.add_command(label="全选", accelerator="Ctrl+A",
                                    command=lambda: self._chat_select_all())
        self.chat.bind("<Button-3>", self._chat_popup)

        self._on_backend_change()

    # ---------------- 记忆管理（改进 23/24/16） ----------------
    def _memory_manager(self) -> None:
        """查看 / 搜索 / 修改 / 删除 / 标记 / 导出记忆。"""
        win = tk.Toplevel(self.root)
        win.title("记忆管理")
        win.transient(self.root)
        win.geometry("980x560")
        style_dialog(win)

        top = ttk.Frame(win, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="搜索", font=FONT_UI).pack(side="left")
        keyword_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=keyword_var, width=28, font=FONT_UI)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda event: refresh())

        columns = ("target", "content", "importance", "now", "retention", "state", "hits")
        tree = ttk.Treeview(win, columns=columns, show="headings", height=16)
        for key, label, width in (("target", "类型", 70), ("content", "内容", 380),
                                  ("importance", "重要度", 70), ("now", "当前有效", 80),
                                  ("retention", "保留策略", 80), ("state", "状态", 70),
                                  ("hits", "被访问", 60)):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="w" if key == "content" else "center")
        tree.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        rows: dict[str, dict] = {}

        def refresh() -> None:
            tree.delete(*tree.get_children())
            rows.clear()
            try:
                items = self.companion.memory.search_texts(keyword=keyword_var.get().strip())
            except Exception as exc:
                self.status_var.set(f"读取记忆失败：{exc}")
                return
            state = {f"{r['target']}#{r['id']}": r
                     for r in self.companion.memory.decay_state()}
            for item in items:
                key = f"{item['target']}#{item['id']}"
                info = state.get(key, {})
                rows[key] = item
                content = item.get("summary") or item.get("content") or ""
                tree.insert("", "end", iid=key, values=(
                    "情景" if item["target"] == "episodic" else "语义",
                    content[:60],
                    f"{float(item.get('importance') or 0):.1f}",
                    f"{float(info.get('now') or 0):.2f}",
                    item.get("retention") or "normal",
                    "冷存储" if int(item.get("archived") or 0) else "活跃",
                    int(item.get("access_count") or 0),
                ))
            win.title(f"记忆管理（{len(items)} 条）")

        def selected() -> tuple[str, int] | None:
            picked = tree.selection()
            if not picked:
                self.status_var.set("先在列表里选一条记忆")
                return None
            target, raw_id = picked[0].split("#")
            return target, int(raw_id)

        def set_retention(retention: str, label: str) -> None:
            who = selected()
            if not who:
                return
            self.companion.memory.set_retention(who[0], who[1], retention)
            refresh()
            self.status_var.set(f"已把 {who[0]}#{who[1]} 标记为「{label}」")

        def wake() -> None:
            who = selected()
            if not who:
                return
            self.companion.memory.set_archived(who[0], who[1], False, "手动唤醒")
            refresh()
            self.status_var.set(f"已唤醒 {who[0]}#{who[1]}")

        def edit() -> None:
            who = selected()
            if not who:
                return
            item = rows.get(f"{who[0]}#{who[1]}", {})
            field = "summary" if who[0] == "episodic" else "content"
            old = item.get(field) or ""
            new = simpledialog.askstring("修改记忆", "改成：", initialvalue=old, parent=win)
            if new and new != old:
                self.companion.memory.update_entry(who[0], who[1], **{field: new})
                refresh()
                self.status_var.set("已修改，并记入审计日志")

        def remove() -> None:
            who = selected()
            if not who:
                return
            item = rows.get(f"{who[0]}#{who[1]}", {})
            preview = (item.get("summary") or item.get("content") or "")[:50]
            if not messagebox.askyesno("确认删除", f"要删除这条记忆吗？\n\n{preview}\n\n删除后无法恢复。",
                                       parent=win):
                return
            self.companion.memory.delete_entry(who[0], who[1])
            refresh()
            self.status_var.set("已删除，并记入审计日志")

        def export(fmt: str) -> None:
            stamp = pathlib.Path("samples")
            path = stamp / f"{__import__('datetime').datetime.now():%Y%m%d-%H%M%S}-记忆导出.{ 'md' if fmt == 'md' else 'json'}"
            try:
                out = self.companion.memory.export_memories(
                    fmt, keyword=keyword_var.get().strip(), redact=False, path=path)
            except Exception as exc:
                self.status_var.set(f"导出失败：{exc}")
                return
            self.status_var.set(f"已导出 {out['count']} 条 → {out['path']}")
            messagebox.showinfo("导出完成", f"已导出 {out['count']} 条：\n{out['path']}", parent=win)

        bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        ttk.Button(bar, text="搜索", command=refresh).pack(side="left")
        ttk.Button(bar, text="刷新", command=refresh).pack(side="left", padx=4)
        ttk.Button(bar, text="永久重要", command=lambda: set_retention("permanent", "永久重要")
                   ).pack(side="left", padx=(12, 4))
        ttk.Button(bar, text="暂时保留", command=lambda: set_retention("temporary", "暂时保留")
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="可遗忘", command=lambda: set_retention("forgettable", "可遗忘")
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="恢复正常", command=lambda: set_retention("normal", "正常")
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="唤醒", command=wake).pack(side="left", padx=(12, 4))
        ttk.Button(bar, text="修改", command=edit).pack(side="left", padx=4)
        ttk.Button(bar, text="删除", command=remove).pack(side="left", padx=4)
        ttk.Button(bar, text="导出 JSON", command=lambda: export("json")).pack(side="right")
        ttk.Button(bar, text="导出 Markdown", command=lambda: export("md")
                   ).pack(side="right", padx=4)

        refresh()
        center_window(win, 980, 560)
        win.grab_set()

    # ---------------- 工具 / MCP / 云端调用查看（模块三） ----------------
    def _tool_calls_window(self) -> None:
        """看工具调用、MCP 服务器状态、云端外发审计——出问题时就靠它。"""
        win = tk.Toplevel(self.root)
        win.title("工具调用与云端外发（审计）")
        win.transient(self.root)
        win.geometry("960x560")
        style_dialog(win)

        source_var = tk.StringVar(value="工具调用")
        top = ttk.Frame(win, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="查看", font=FONT_UI).pack(side="left")
        for label in ("工具调用", "MCP 服务器", "云端外发"):
            ttk.Radiobutton(top, text=label, value=label, variable=source_var,
                            command=lambda: refresh()).pack(side="left", padx=6)
        ttk.Button(top, text="刷新", width=6, command=lambda: refresh()).pack(side="left",
                                                                              padx=10)
        ttk.Button(top, text="打开日志目录", width=12,
                   command=self._open_log_dir).pack(side="left")

        columns = ("time", "who", "kind", "ok", "ms", "brief")
        tree = ttk.Treeview(win, columns=columns, show="headings", height=12)
        for key, label, width in (("time", "时间", 140), ("who", "技能/服务器", 130),
                                  ("kind", "类型", 80), ("ok", "结果", 60),
                                  ("ms", "耗时", 70), ("brief", "摘要", 420)):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="w" if key == "brief" else "center")
        tree.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        try:
            from gui_theme import COLORS as _C2
        except Exception:
            _C2 = {}
        detail = scrolledtext.ScrolledText(
            win, height=8, font=FONT_META, wrap="word", relief="flat",
            background=_C2.get("bg", "#0d1220"),
            foreground=_C2.get("text", "#E8ECFF"),
            insertbackground=_C2.get("text", "#E8ECFF"))
        detail.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        theme_scroll(detail, _C2)
        records: dict[str, dict] = {}

        def refresh() -> None:
            tree.delete(*tree.get_children())
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            detail.configure(state="disabled")
            records.clear()
            source = source_var.get()
            if source == "MCP 服务器":
                servers = mcp_client.load_servers()
                for name, spec in servers.items():
                    state = "已启用" if spec.get("enabled") else "未启用"
                    children = ", ".join(spec.get("args") or [])
                    key = f"mcp#{name}"
                    records[key] = {"服务器": name, **spec}
                    tree.insert("", "end", iid=key, values=(
                        "-", name, "mcp", state, "-", children or spec.get("command", "")))
                if not servers:
                    tree.insert("", "end", values=("-", "（没有配置 MCP 服务器）", "", "", "", ""))
                return
            if source == "云端外发":
                rows = gateway.recent_calls(200)
                for index, item in enumerate(reversed(rows)):
                    key = f"api#{index}"
                    records[key] = item
                    outbound = json.dumps(item.get("outbound") or {}, ensure_ascii=False)
                    tree.insert("", "end", iid=key, values=(
                        str(item.get("time", ""))[11:19], item.get("api", ""),
                        item.get("purpose", ""),
                        "已发" if item.get("sent") else "被拦",
                        f"{item.get('ms', '-')}", (outbound or item.get("reason", ""))[:80]))
            else:
                rows = executor.recent_calls(200)
                for index, item in enumerate(reversed(rows)):
                    key = f"tool#{index}"
                    records[key] = item
                    brief = str(item.get("result") or item.get("detail") or "")
                    tree.insert("", "end", iid=key, values=(
                        str(item.get("time", ""))[11:19], item.get("skill", ""),
                        item.get("tool", ""), "成功" if item.get("ok") else "失败",
                        f"{item.get('ms', '-')}", brief[:80]))
            if not records:
                tree.insert("", "end", values=("-", "（还没有记录）", "", "", "", ""))

        def on_select(event=None) -> None:
            picked = tree.selection()
            if not picked:
                return
            item = records.get(picked[0])
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            detail.insert("end", json.dumps(item, ensure_ascii=False, indent=2)
                          if item else "（没有详情）")
            detail.configure(state="disabled")

        tree.bind("<<TreeviewSelect>>", on_select)
        ttk.Label(win, text="日志文件：logs/tool_calls.jsonl（工具调用）"
                            "　logs/api_calls.jsonl（云端外发）",
                  font=FONT_META, foreground="#777777",
                  padding=(12, 0, 12, 8)).pack(fill="x")
        refresh()
        center_window(win, 960, 560)
        win.grab_set()

    def _open_log_dir(self) -> None:
        path = pathlib.Path("logs")
        path.mkdir(exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                import os
                os.startfile(str(path.resolve()))       # noqa: S606
            else:
                os.startfile  # type: ignore[attr-defined]
        except Exception as exc:
            self.status_var.set(f"打不开日志目录：{exc}")

    # ---------------- AI 自己的档案（模块二 改进 4） ----------------
    def _ai_profile_window(self) -> None:
        """查看/调整未央自己的档案：名字、语气、性格维度、调整历史（可回滚）。"""
        win = tk.Toplevel(self.root)
        win.title("未央的档案（AI 自己，不是用户画像）")
        win.transient(self.root)
        win.geometry("720x470")
        style_dialog(win)
        profile = self.companion.ai_profile
        status_var = tk.StringVar(value="调整立刻对下一条消息生效；底线不受性格影响")

        top = ttk.Frame(win, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        name_var = tk.StringVar(value=(profile.get("identity") or {}).get("name", "未央"))
        tone_var = tk.StringVar(value=(profile.get("identity") or {}).get("tone", "温柔体贴"))
        ttk.Label(top, text="名字", font=FONT_UI).pack(side="left")
        ttk.Entry(top, textvariable=name_var, width=10, font=FONT_UI).pack(
            side="left", padx=(4, 12))
        ttk.Label(top, text="语气", font=FONT_UI).pack(side="left")
        ttk.Entry(top, textvariable=tone_var, width=12, font=FONT_UI).pack(side="left", padx=4)

        grid = ttk.Frame(win, padding=(12, 6, 12, 4))
        grid.pack(fill="x")
        ttk.Label(grid, text="性格维度（−/＋ 单次 0.05，封顶在配置上下限内）",
                  font=FONT_TITLE).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        value_labels: dict[str, tk.StringVar] = {}

        def refresh_values() -> None:
            for key, var in value_labels.items():
                var.set(f"{float(profile['traits'].get(key, 0.5)):.2f}")

        def bump(key: str, delta: float) -> None:
            current = float(profile["traits"].get(key, 0.5))
            try:
                ai_profile_engine.set_trait(profile, key, current + delta,
                                            turn=self.companion.turn)
            except Exception as exc:
                status_var.set(f"调整失败：{exc}")
                return
            self.companion.save_ai_profile()
            refresh_values()
            status_var.set(f"{ai_profile_engine.DIM_LABELS.get(key, key)} → "
                           f"{float(profile['traits'][key]):.2f}（下一条消息生效）")

        for index, key in enumerate(ai_profile_engine.DIMENSIONS):
            var = tk.StringVar()
            value_labels[key] = var
            row, col = 1 + index // 2, (index % 2) * 4
            ttk.Label(grid, text=ai_profile_engine.DIM_LABELS.get(key, key),
                      font=FONT_UI).grid(row=row, column=col, sticky="w", pady=2)
            ttk.Button(grid, text="−", width=3,
                       command=lambda k=key: bump(k, -0.05)).grid(row=row, column=col + 1,
                                                                  padx=4)
            ttk.Label(grid, textvariable=var, font=FONT_UI, width=5).grid(
                row=row, column=col + 2)
            ttk.Button(grid, text="＋", width=3,
                       command=lambda k=key: bump(k, 0.05)).grid(row=row, column=col + 3,
                                                                 padx=4)
        refresh_values()

        try:
            from gui_theme import COLORS as _C3
        except Exception:
            _C3 = {}
        history_box = scrolledtext.ScrolledText(
            win, height=8, font=FONT_META, wrap="word", relief="flat",
            background=_C3.get("bg", "#0d1220"),
            foreground=_C3.get("text", "#E8ECFF"),
            insertbackground=_C3.get("text", "#E8ECFF"))
        history_box.pack(fill="both", expand=True, padx=12, pady=6)
        theme_scroll(history_box, _C3)

        def refresh_history() -> None:
            history_box.configure(state="normal")
            history_box.delete("1.0", "end")
            history_box.insert("end", ai_profile_engine.describe(profile) + "\n\n")
            history_box.insert("end", "调整历史（最近的在下）\n")
            history_box.insert("end", ai_profile_engine.history_table(profile, limit=15))
            history_box.configure(state="disabled")

        bar = ttk.Frame(win, padding=(12, 0, 12, 6))
        bar.pack(fill="x")

        def save_identity() -> None:
            profile.setdefault("identity", {})["name"] = name_var.get().strip() or "未央"
            profile["identity"]["tone"] = tone_var.get().strip() or "温柔体贴"
            self.companion.save_ai_profile()
            refresh_history()
            status_var.set("名字与语气已保存")

        def undo() -> None:
            result = ai_profile_engine.rollback(profile, 1)
            self.companion.save_ai_profile()
            refresh_values()
            refresh_history()
            status_var.set(result["reason"])

        def reset() -> None:
            if not messagebox.askyesno("恢复默认", "把性格恢复成默认值？（历史仍保留）",
                                       parent=win):
                return
            ai_profile_engine.reset(profile, self.companion.turn)
            self.companion.save_ai_profile()
            refresh_values()
            refresh_history()
            status_var.set("已恢复默认性格")

        ttk.Button(bar, text="保存名字/语气", command=save_identity).pack(side="left")
        ttk.Button(bar, text="回滚上次调整", command=undo).pack(side="left", padx=6)
        ttk.Button(bar, text="恢复默认", command=reset).pack(side="left")
        ttk.Label(win, textvariable=status_var, font=FONT_META, foreground="#555555",
                  padding=(12, 0, 12, 10), wraplength=680, justify="left").pack(fill="x")
        refresh_history()
        center_window(win, 720, 470)
        win.grab_set()

    # ---------------- 模型服务（模块四） ----------------
    def _on_close(self) -> None:
        """关闭前台窗口：把本程序启动的后台服务一起收掉，不留残留进程。

        ⚠ 2026-10-21 补：**两处都要收**。
          `self.services.stop_all()` 只停**这个 manager 实例**记着的服务；
          而 `autostart.ensure_*_service()` 每次会**新建一个 ServiceManager**，
          它起的服务这个实例**根本不知道**——不调 `stop_started()` 就会留孤儿
          （实测：一键拉起之后关窗口，8081/8083 还活着）。
          `stop_started()` 只收 `_STARTED_BY_US`，**手动开的不会被误杀**。
        """
        try:
            _stop_on_exit = bool(getattr(self, "stop_services_on_exit", True))
            try:
                from services import models_config as _mc
                _stop_on_exit = _stop_on_exit and bool(_mc.switch("stop_on_exit"))
            except Exception:
                pass
            if _stop_on_exit:
                self.services.stop_all()
                try:
                    from services import autostart as _auto
                    _auto.stop_started(verbose=False)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _service_window(self) -> None:
        """统一启动器：选模型、调参数、存预设、启停服务。"""
        if self.svc_win is not None and self.svc_win.winfo_exists():
            self.svc_win.lift()
            return
        win = tk.Toplevel(self.root)
        self.svc_win = win
        win.title("模型服务（模块四：统一启动 / 预设 / 参数）")
        win.transient(self.root)
        win.geometry("880x430")
        style_dialog(win)

        name, spec = self.presets.default()
        v = {
            "preset": tk.StringVar(value=name),
            "model": tk.StringVar(value=spec.get("model", "")),
            "kind": tk.StringVar(value="对话" if spec.get("kind") != "embed" else "嵌入"),
            "port": tk.StringVar(value=str(spec.get("port", 8080))),
            "ctx": tk.StringVar(value=str(spec.get("ctx", 4096))),
            "ngl": tk.StringVar(value=str(spec.get("ngl", 15))),
            "threads": tk.StringVar(value=str(spec.get("threads", 8))),
            "kv": tk.StringVar(value=str(spec.get("kv", "q8_0"))),
            "batch": tk.StringVar(value=str(spec.get("batch", 128))),
            "ubatch": tk.StringVar(value=str(spec.get("ubatch", 64))),
            "parallel": tk.StringVar(value=str(spec.get("parallel", 1))),
            "jinja": tk.BooleanVar(value=bool(spec.get("jinja", True))),
            "extra": tk.StringVar(value=str(spec.get("extra", ""))),
            "status": tk.StringVar(value="就绪"),
            "on_exit": tk.BooleanVar(value=self.stop_services_on_exit),
        }
        self.svc_vars = v

        top = ttk.Frame(win, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="预设", font=FONT_UI).pack(side="left")
        preset_box = ttk.Combobox(top, textvariable=v["preset"], state="readonly",
                                  values=self.presets.names(), width=26, font=FONT_UI)
        preset_box.pack(side="left", padx=6)
        self.svc_preset_box = preset_box
        ttk.Button(top, text="载入", width=6,
                   command=lambda: self._svc_load(preset_box.get())).pack(side="left")
        ttk.Button(top, text="另存为…", width=9,
                   command=self._svc_save_as).pack(side="left", padx=4)
        ttk.Button(top, text="删除", width=6, command=self._svc_delete).pack(side="left")
        ttk.Button(top, text="设为默认", width=9,
                   command=lambda: self._svc_set_default(preset_box.get())).pack(side="left", padx=4)

        mid = ttk.Frame(win, padding=(12, 4, 12, 4))
        mid.pack(fill="x")
        ttk.Label(mid, text="模型", font=FONT_UI).grid(row=0, column=0, sticky="w")
        model_box = ttk.Combobox(mid, textvariable=v["model"], width=78, font=FONT_UI,
                                 values=[m["path"] for m in self.services.models()])
        model_box.grid(row=0, column=1, columnspan=7, sticky="we", padx=6, pady=3)
        self.svc_model_box = model_box

        rows = [("类型", "kind", 6), ("端口", "port", 7), ("上下文", "ctx", 8),
                ("显存层数", "ngl", 6), ("线程", "threads", 6), ("KV 类型", "kv", 7),
                ("批大小", "batch", 7), ("微批", "ubatch", 7), ("并行", "parallel", 6)]
        grid = ttk.Frame(win, padding=(12, 0, 12, 4))
        grid.pack(fill="x")
        for index, (label, key, width) in enumerate(rows):
            row, col = divmod(index, 3)
            ttk.Label(grid, text=label, font=FONT_UI).grid(row=row, column=col * 2,
                                                           sticky="w", pady=3)
            if key == "kind":
                ttk.Combobox(grid, textvariable=v[key], values=["对话", "嵌入"], width=width,
                             state="readonly", font=FONT_UI).grid(
                    row=row, column=col * 2 + 1, sticky="w", padx=(6, 18))
            else:
                ttk.Entry(grid, textvariable=v[key], width=width,
                          font=FONT_UI).grid(row=row, column=col * 2 + 1, sticky="w",
                                             padx=(6, 18))

        extra = ttk.Frame(win, padding=(12, 0, 12, 4))
        extra.pack(fill="x")
        ttk.Checkbutton(extra, text="用模型自带对话模板（--jinja）",
                        variable=v["jinja"]).pack(side="left")
        ttk.Label(extra, text="额外参数", font=FONT_UI).pack(side="left", padx=(14, 4))
        ttk.Entry(extra, textvariable=v["extra"], font=FONT_UI).pack(
            side="left", fill="x", expand=True)

        bar = ttk.Frame(win, padding=(12, 4, 12, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="扫描模型", width=10, command=self._svc_scan).pack(side="left")
        ttk.Button(bar, text="启动", width=8, command=self._svc_start).pack(side="left", padx=4)
        ttk.Button(bar, text="停止", width=8, command=self._svc_stop).pack(side="left")
        ttk.Button(bar, text="重启", width=8, command=self._svc_restart).pack(side="left", padx=4)
        ttk.Button(bar, text="查看日志", width=10, command=self._svc_log).pack(side="left")
        ttk.Checkbutton(bar, text="关窗口时停止我启动的服务", variable=v["on_exit"],
                        command=self._svc_save_exit_flag).pack(side="right")

        ttk.Label(win, textvariable=v["status"], font=FONT_META, foreground="#555555",
                  padding=(12, 0, 12, 10), wraplength=840, justify="left").pack(fill="x")

        self._svc_refresh()
        center_window(win, 880, 430)
        win.protocol("WM_DELETE_WINDOW", self._svc_close)

    def _svc_spec(self) -> dict:
        v = self.svc_vars
        spec = {
            "kind": "embed" if v["kind"].get() == "嵌入" else "chat",
            "model": v["model"].get().strip(),
            "port": v["port"].get(), "ctx": v["ctx"].get(), "ngl": v["ngl"].get(),
            "threads": v["threads"].get(), "kv": v["kv"].get(), "batch": v["batch"].get(),
            "ubatch": v["ubatch"].get(), "parallel": v["parallel"].get(),
            "jinja": bool(v["jinja"].get()), "extra": v["extra"].get(),
            "host": "127.0.0.1",
        }
        for key in ("port", "ctx", "ngl", "threads", "batch", "ubatch", "parallel"):
            try:
                spec[key] = int(str(spec[key]).strip() or 0)
            except ValueError:
                raise ServiceError(f"参数「{key}」要填数字")
        return spec

    def _svc_fill(self, spec: dict) -> None:
        v = self.svc_vars
        v["model"].set(spec.get("model", ""))
        v["kind"].set("嵌入" if spec.get("kind") == "embed" else "对话")
        for key in ("port", "ctx", "ngl", "threads", "kv", "batch", "ubatch",
                    "parallel", "extra"):
            if key in spec:
                v[key].set(str(spec[key]))
        v["jinja"].set(bool(spec.get("jinja", spec.get("kind") != "embed")))

    def _svc_load(self, name: str) -> None:
        try:
            self._svc_fill(self.presets.get(name))
            self.svc_vars["status"].set(f"已载入预设「{name}」")
        except Exception as exc:
            self.svc_vars["status"].set(f"载入失败：{exc}")

    def _svc_save_as(self) -> None:
        name = simpledialog.askstring("保存预设", "预设名：", parent=self.svc_win,
                                      initialvalue=self.svc_vars["preset"].get())
        if not name:
            return
        try:
            self.presets.upsert(name, self._svc_spec())
            self.svc_vars["preset"].set(name)
            self._svc_reload_names()
            self.svc_vars["status"].set(f"预设「{name}」已保存到 config/presets.json")
        except Exception as exc:
            self.svc_vars["status"].set(f"保存失败：{exc}")

    def _svc_delete(self) -> None:
        name = self.svc_vars["preset"].get()
        if not messagebox.askyesno("删除预设", f"要删除预设「{name}」吗？", parent=self.svc_win):
            return
        self.presets.delete(name)
        self._svc_reload_names()
        self.svc_vars["status"].set(f"预设「{name}」已删除")

    def _svc_set_default(self, name: str) -> None:
        try:
            self.presets.set_default(name)
            self.svc_vars["status"].set(f"「{name}」已设为默认预设")
        except Exception as exc:
            self.svc_vars["status"].set(f"设置失败：{exc}")

    def _svc_reload_names(self) -> None:
        try:
            self.svc_preset_box.configure(values=self.presets.names())
        except Exception:
            pass

    def _svc_scan(self) -> None:
        models = self.services.models()
        try:
            self.svc_model_box.configure(values=[m["path"] for m in models])
            if not self.svc_vars["model"].get() and models:
                self.svc_vars["model"].set(models[0]["path"])
        except Exception:
            pass
        self.svc_vars["status"].set(
            f"扫描到 {len(models)} 个 GGUF 模型"
            + ("：" + "、".join(m["name"] for m in models[:4]) if models else ""))

    def _svc_save_exit_flag(self) -> None:
        self.stop_services_on_exit = bool(self.svc_vars["on_exit"].get())
        state = dict(self.ui_state)
        state["stop_services_on_exit"] = self.stop_services_on_exit
        tts_engine.save_ui_state(state)

    def _svc_start(self) -> None:
        try:
            spec = self._svc_spec()
        except Exception as exc:
            self.svc_vars["status"].set(str(exc))
            return
        self.svc_vars["status"].set(f"正在启动（端口 {spec['port']}）…模型大的话要等十几秒")

        def run():
            try:
                handle = self.services.start(spec)
                self.queue.put(("svc_ok", "", f"已启动 {spec['kind']}（PID {handle['pid']}）",
                                spec["port"]))
            except ServiceError as exc:
                self.queue.put(("svc_err", "", str(exc), spec.get("port", 0)))

        threading.Thread(target=run, daemon=True).start()

    def _svc_stop(self) -> None:
        try:
            port = int(self.svc_vars["port"].get())
        except ValueError:
            self.svc_vars["status"].set("端口要填数字")
            return
        stopped = self.services.stop(port)
        self.svc_vars["status"].set(f"端口 {port} " + ("已停止" if stopped else "没有本程序启动的服务"))
        self._svc_refresh()

    def _svc_restart(self) -> None:
        try:
            port = int(self.svc_vars["port"].get())
        except ValueError:
            self.svc_vars["status"].set("端口要填数字")
            return
        self.services.stop(port)
        self.root.after(1500, self._svc_start)

    def _svc_log(self) -> None:
        try:
            port = int(self.svc_vars["port"].get())
        except ValueError:
            port = 0
        text = self.services.tail_log(port, lines=60)
        self._show_text(f"服务日志（端口 {port}）", text)

    def _svc_refresh(self) -> None:
        if self.svc_win is None or not self.svc_win.winfo_exists():
            return
        parts = []
        for item in self.services.status([8080, 8081]):
            state = "健康 ✓" if item["healthy"] else ("在监听" if item["listening"] else "未运行")
            pid = f"（PID {item['pid']}）" if item["pid"] else ""
            managed = "｜本程序启动" if item["managed"] else ""
            parts.append(f"{item['port']} {state}{pid}{managed}")
        self.svc_vars["status"].set("　｜　".join(parts))
        self.root.after(3000, self._svc_refresh)

    def _svc_close(self) -> None:
        if self.svc_win is not None:
            try:
                self.svc_win.destroy()
            except Exception:
                pass
        self.svc_win = None

    # ---------------- 清除记忆（调试用，必须二次确认） ----------------
    def _wipe_dialog(self) -> None:
        info = self.companion.wipe_summary()
        dlg = tk.Toplevel(self.root)
        dlg.title("清除记忆（调试用）")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        style_dialog(dlg)

        body = ttk.Frame(dlg, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="当前数据", font=FONT_TITLE).pack(anchor="w")
        ttk.Label(body, font=FONT_UI, justify="left",
                  text=(f"情景记忆 {info['episodes']} 轮　语义记忆 {info['semantic']} 条"
                        f"　向量索引 {info['vectors']} 条\n"
                        f"用户画像 {info['profile']} 条　价值观 {info['values']} 条"
                        f"　待办 {info['reminders']} 条")
                  ).pack(anchor="w", pady=(2, 12))

        mem_var = tk.BooleanVar(value=True)
        pro_var = tk.BooleanVar(value=True)
        val_var = tk.BooleanVar(value=False)      # 默认保留价值观：靠养成，别误删
        chat_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="长期记忆（情景／语义／反馈／提醒 ＋ 向量索引）",
                        variable=mem_var).pack(anchor="w")
        ttk.Checkbutton(body, text="用户画像（profile.json）",
                        variable=pro_var).pack(anchor="w")
        ttk.Checkbutton(body, text="连价值观一起清除（values.yaml／scenarios／preferences）",
                        variable=val_var).pack(anchor="w")
        ttk.Checkbutton(body, text="同时清空对话窗口显示",
                        variable=chat_var).pack(anchor="w")
        ttk.Label(body, text=f"默认会保留价值观（当前 {info['values']} 条）；"
                             f"短期上下文一定会被清空。删除后无法恢复。",
                  font=FONT_META, foreground="#c01c28").pack(anchor="w", pady=(10, 10))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x")

        def confirm() -> None:
            report = self.companion.wipe(mem_var.get(), pro_var.get(), val_var.get())
            clear_chat = chat_var.get()
            dlg.destroy()
            self._after_wipe(report, clear_chat)

        ttk.Button(buttons, text="取消", command=dlg.destroy).pack(side="right")
        ttk.Button(buttons, text="确认清除", command=confirm).pack(side="right", padx=6)

        dlg.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dlg.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dlg.winfo_height()) // 3)
        dlg.geometry(f"+{x}+{y}")
        dlg.grab_set()          # 模态：必须明确选择取消或确认

    def _after_wipe(self, report: dict, clear_chat: bool) -> None:
        tts_engine.stop_playback()
        self._speech_token += 1
        self._speech_state = "idle"
        self.pending_reply = None
        self._pending_line = None
        self._refresh_speech_button()
        if clear_chat:
            self.replies.clear()
            self.chat.delete("1.0", "end")
        parts = []
        mem = report.get("memory") or {}
        if mem:
            parts.append(f"情景 {mem.get('episodic', 0)} 轮、语义 {mem.get('semantic', 0)} 条"
                         f"、反馈 {mem.get('feedback', 0)} 条")
        if "vectors" in report:
            parts.append(f"向量 {report['vectors']} 条")
        if "profile" in report:
            parts.append(f"画像 {report['profile']} 条")
        if "values" in report:
            parts.append(f"价值观 {report['values']} 条")
        summary = "；".join(parts) if parts else "没有可清的数据"
        kept = ""
        if "values" not in report:
            kept = f"（价值观保留：{len(self.companion.store.get('values', []))} 条）"
        self._append("系统", f"已清除：{summary}。{kept}现在是干净状态。\n", "meta")
        self.status_var.set("记忆已清除（短期上下文也清空了）")

    # ---------------- 只读与回听 ----------------
    def _block_typing(self, event):
        """让对话区只读：允许选中/复制/翻页，拦截其它按键。"""
        ctrl = bool(event.state & 0x4)
        if ctrl and event.keysym.lower() in ("c", "a"):
            return None
        if event.keysym in ("Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End"):
            return None
        return "break"

    def _chat_select_all(self) -> None:
        """全选对话内容（只读控件不能靠 Ctrl+A 时用右键菜单）。"""
        try:
            self.chat.tag_add("sel", "1.0", "end-1c")
            self.chat.focus_set()
        except Exception:
            pass

    def _chat_popup(self, event):
        """右键菜单：复制 / 全选。

        ⚠ 托盘式的"只读 Text"没有右键菜单时，很多人**只会用右键复制**，
          而 Tk 的 Text 默认没有——这是"选不中/复制不了"体感的另一半来源。
        """
        try:
            has_sel = bool(self.chat.tag_ranges("sel"))
            self._chat_menu.entryconfigure(0, state="normal" if has_sel else "disabled")
            self._chat_menu.tk_popup(event.x_root, event.y_root)
        except Exception:
            pass
        finally:
            try:
                self._chat_menu.grab_release()
            except Exception:
                pass

    def _speech_text(self, reply: str, text_lang: str | None = None,
                     voice_lang: str | None = None) -> tuple[str, bool]:
        """算出"该拿去朗读的文本"。文本语言≠朗读语言时先翻译（只影响语音）。

        返回 (朗读文本, 是否用了译文)
        注意：语言值要在主线程取好再传进来——Tk 变量跨线程读取会直接报错。
        """
        text_lang = text_lang or self._text_lang()
        voice_lang = voice_lang or self._voice_lang()
        if not translate_engine.needs_translation(text_lang, voice_lang):
            return reply, False
        key = (reply, voice_lang)
        if key not in self.speech_cache:
            self.speech_cache[key] = translate_engine.to_language(
                self.companion.generator, reply, voice_lang)
        translated = self.speech_cache[key]
        return translated, translated.strip() != reply.strip()

    def _refresh_speech_button(self) -> None:
        busy = self._speech_state != "idle"
        try:
            self.replay_button.configure(text="⏹ 停止朗读" if busy else "🔊 重听上一句")
        except Exception:
            pass
        # 播放联动跟着一起刷新（合成中与播放中都要保持"隐形"）
        self._refresh_replay_playback()

    def _speak(self, text: str, tag: str = "", reveal: bool = False,
               index: int | None = None) -> None:
        """统一的朗读入口：同一时刻只会有一段声音，新的请求会打断旧的。

        reveal=True 表示这段语音对应"还没显示出来的回复"：合成好了再放文本，
        让文本和声音同时出现（避免文本先出、声音没动静被误判成合成失败）。
        index 是这条回复在 self.replies 里的序号：用来把那条「回听」标成
        "播放中"（字色融进气泡），读完后它才按常态/1 秒规则显出来。
        """
        cfg = self._voice_cfg()
        if not cfg:
            self.status_var.set("当前是「不语音」，先在左上角选个语音后端")
            return
        tts_engine.stop_playback()          # 立刻打断正在播的
        self._speech_token += 1             # 旧的在途合成任务作废
        token = self._speech_token
        self._active_replay_link = f"replay{index}" if index is not None else None
        # 语言设置在主线程序取好：Tk 变量不能在子线程里读
        text_lang, voice_lang = self._text_lang(), self._voice_lang()
        self._speech_state = "working"
        self._refresh_speech_button()
        self.status_var.set(f"正在合成{tag}…")

        def run():
            try:
                spoken, translated = self._speech_text(text, text_lang, voice_lang)
                path = tts_engine.synthesize(spoken, cfg)
                self.queue.put(("speech_ready",
                                "1" if translated else "", str(path),
                                {"token": token, "reveal": reveal}))
            except Exception as exc:
                self.queue.put(("tts_err", "", str(exc),
                                {"token": token, "reveal": reveal}))

        threading.Thread(target=run, daemon=True).start()

    def _replay(self, index: int) -> str:
        """重读第 index 条回复（用当前语音设置重新合成，方便换音色/语言后对比）。"""
        if not (0 <= index < len(self.replies)):
            return "break"
        self._speak(self.replies[index], f"第 {index + 1} 条", index=index)
        return "break"

    def _replay_last(self) -> None:
        # 正在播（或正在合成）时，这个按钮变成"停止"
        if self._speech_state != "idle":
            tts_engine.stop_playback()
            self._speech_token += 1         # 作废在途合成
            self._speech_state = "idle"
            self._refresh_speech_button()
            self.status_var.set("已停止朗读")
            return
        if not self.replies:
            self.status_var.set("还没有可以重读的回复")
            return
        self._speak(self.replies[-1], f"第 {len(self.replies)} 条")

    # ---------------- 语音设置 ----------------
    def _text_lang(self) -> str:
        """未央用什么语言写。"""
        return TEXT_LANGS.get(self.text_lang_var.get(), "zh")

    def _voice_lang(self) -> str:
        """语音念成什么语言；"自动"表示跟随文本语言。"""
        chosen = VOICE_LANGS.get(self.voice_lang_var.get())
        return chosen or self._text_lang()

    def _on_text_lang_change(self, event=None) -> None:
        """文本语言变化：只影响"怎么写"；朗读语言若为"自动"则自然跟随。"""
        self._load_edge_voices()
        self._save_state()
        txt, voice = self.text_lang_var.get(), self.voice_lang_var.get()
        self.status_var.set(f"文本语言：{txt}　朗读语言：{voice}"
                            f"（{'跟随文本' if voice == '自动' else '独立指定'}）")

    def _on_voice_lang_change(self, event=None) -> None:
        """朗读语言变化：只影响"怎么念"，不动文本语言。"""
        self._load_edge_voices()
        self._save_state()
        self.status_var.set(f"朗读语言：{self.voice_lang_var.get()}"
                            f"（实际用 {self._voice_lang()}）")

    def _on_backend_change(self, event=None) -> None:
        backend = self.backend_var.get()
        is_edge = backend == "Edge TTS"
        self.edge_box.configure(state="normal" if is_edge else "disabled")
        if is_edge:
            self._load_edge_voices()
        self._save_state()
        self.status_var.set(f"语音：{backend}")

    def _load_edge_voices(self, refresh: bool = False) -> None:
        try:
            lang = self._voice_lang()
            voices = tts_engine.list_edge_voices(
                locale_prefix=EDGE_PREFIX.get(lang, "zh-"), refresh=refresh)
            names = [f"{v['name']}" + (f"（{v['gender']}·{v['note']}）" if v.get("note")
                                       else f"（{v['gender']}）") for v in voices]
            self.edge_box.configure(values=names)
            current = self.edge_voice_var.get()
            if current and not any(n.startswith(current) for n in names):
                self.edge_voice_var.set(names[0].split("（")[0] if names else current)
        except Exception as exc:
            self.status_var.set(f"取声音列表失败：{exc}")

    def _refresh_voices(self) -> None:
        if self.backend_var.get() != "Edge TTS":
            self._show_text("提示", "刷新声音只对 Edge TTS 有意义。")
            return
        self._load_edge_voices(refresh=True)
        self.status_var.set("声音列表已刷新")

    def _save_state(self) -> None:
        voice = self.edge_voice_var.get().split("（")[0].strip()
        tts_engine.save_ui_state({
            "backend": self.backend_var.get(),
            "edge_voice": voice,
            "text_lang": self.text_lang_var.get(),
            "voice_lang": self.voice_lang_var.get(),
            "autoplay": bool(self.autoplay_var.get()),
        })

    def _voice_cfg(self) -> dict | None:
        """把界面上的选择拼成一份 TTS 配置。返回 None 表示不语音。"""
        backend = BACKEND_KEYS.get(self.backend_var.get())
        self._save_state()
        if not backend:
            return None
        cfg = dict(self.voice_config)
        cfg["backend"] = backend
        cfg.setdefault("output_dir", "voice/out")
        cfg.setdefault("text_max_chars", 300)
        if backend == "edge":
            edge = dict(cfg.get("edge") or {})
            edge["voice"] = self.edge_voice_var.get().split("（")[0].strip()
            cfg["edge"] = edge
        elif backend == "gsv":
            gsv = dict(cfg.get("gsv") or {})
            # 朗读语言：独立于文本语言；"自动"交给服务自己识别
            voice_lang = VOICE_LANGS.get(self.voice_lang_var.get())
            lang = voice_lang or "auto"
            gsv["text_lang"] = lang
            gsv["text_language"] = lang
            # 参考音频的语言是固定的（ja），不能被合成语言覆盖
            gsv.setdefault("prompt_lang", "ja")
            gsv.setdefault("prompt_language", "ja")
            cfg["gsv"] = gsv
        return cfg

    def _try_voice(self) -> None:
        cfg = self._voice_cfg()
        if not cfg:
            self._show_text("提示", "先在左上角选一个语音后端。")
            return
        # 试听用「朗读语言」的句子，这样一听就知道念的是哪国话
        lang = self._voice_lang()
        sample = SAMPLE_TEXT.get(lang, SAMPLE_TEXT["zh"])
        self._speak(sample, f"试听（{lang}）")

    # ---------------- 对话 ----------------
    def _open_settings(self) -> None:
        """打开独立设置窗口（参考 Cyrene-Agent 的分栏设置界面）。"""
        try:
            from gui_settings import open_settings
        except Exception as exc:
            messagebox.showwarning("设置", f"设置窗口不可用：{exc}")
            return
        try:
            open_settings(self.root, ui_state=self.ui_state,
                          voice_config=self.voice_config, companion=self.companion,
                          manager=ServiceManager(), on_change=self._on_setting_changed,
                          on_wipe=self._wipe_dialog)
        except Exception as exc:
            messagebox.showwarning("设置", f"打开设置失败：{exc}")

    def _apply_fonts(self) -> None:
        """按设置里的字体项刷新界面与聊天区字体（改完立即生效）。

        ## ⚠ 2026-10-27：原来只改了 ttk 的 style，**tk 控件（Label/Button/Checkbutton）
        根本没动** —— 那些控件是建的时候用 `font=FONT_UI` 这类元组固化的，
        `style.configure` 对它们无效。所以「界面字号」以前只对下拉框/按钮部分生效，
        看起来像"改了没用"。

        现在两步：
          ① 重新 `pick_font(base=size)` 更新 `gui_theme` 的字体元组
          ② `sync_fonts(globals())` 灌回本模块，并用 `restyle_tree` 走一遍控件树
             （把"老字体元组"映射到"新字体元组"，直接改每个控件的 `font`）
        """
        try:
            from gui_theme import pick_font as _pick, sync_fonts, restyle_tree
            import gui_theme as _gt
        except Exception:
            _pick = sync_fonts = restyle_tree = None
        try:
            family = str(self.ui_state.get("ui_font_family") or "Microsoft YaHei UI")
            size = int(self.ui_state.get("ui_font_size") or 10)
            # ① 更新主题字体元组（base = 界面字号 - 1，因为 FONT_UI = base + 1）
            if _pick is not None:
                old_fonts = {"FONT_UI": globals().get("FONT_UI"),
                             "FONT_SMALL": globals().get("FONT_SMALL"),
                             "FONT_TITLE": globals().get("FONT_TITLE")}
                try:
                    _pick(self.root, base=max(7, size - 1), family=family)
                    # ② 灌回本模块 + 走一遍控件树
                    mapping = {}
                    for key, old_val in old_fonts.items():
                        new_val = getattr(_gt, key, None)
                        if old_val is not None and new_val is not None \
                                and tuple(old_val) != tuple(new_val):
                            mapping[tuple(old_val)] = new_val
                    sync_fonts(globals())
                    if mapping and restyle_tree is not None:
                        restyle_tree(self.root, mapping)
                except Exception as exc:
                    print(f"（重绘字体时出错，忽略：{exc}）")
            chat_family = str(self.ui_state.get("chat_font_family") or family)
            chat_size = int(self.ui_state.get("chat_font_size") or 12)
            style = ttk.Style(self.root)
            for name in ("TLabel", "TButton", "TEntry", "TCombobox", "TCheckbutton",
                         "TLabelframe.Label"):
                try:
                    style.configure(name, font=(family, size))
                except Exception:
                    pass
            self.chat.configure(font=(chat_family, chat_size))
            self.entry.configure(font=(chat_family, chat_size))
        except Exception:
            pass

    def _on_setting_changed(self, key: str, value) -> None:
        """设置窗口里改了某项 → 复用主界面原有的处理逻辑。"""
        if key == "backend":
            try:
                self._on_backend_change(None)
            except Exception:
                pass
        elif key == "text_lang":
            try:
                self.text_lang_var.set(str(value))
            except Exception:
                pass
        elif key == "speak_lang":
            try:
                self.speak_lang_var.set(str(value))
            except Exception:
                pass
        elif key == "edge_voice":
            try:
                self.edge_voice_var.set(str(value))
            except Exception:
                pass
        elif key == "auto_speak":
            try:
                self.auto_speak_var.set(bool(value))
            except Exception:
                pass
        elif key in ("chat_font_size", "chat_font_family",
                     "ui_font_family", "ui_font_size"):
            self._apply_fonts()
        elif key == "show_meta":
            self.show_meta = str(value) != "隐藏"
        elif key == "show_replay":
            self.show_replay = str(value) != "隐藏"
        elif key == "inject_memory":
            try:
                self.companion.inject_memory = str(value) != "关闭"
            except Exception:
                pass
        try:
            self._save_ui_state()
        except Exception:
            pass

    def _on_send(self, event=None):
        """发送入口（按钮和回车都走这里）。

        ⚠ **必须 `return "break"`**：输入框换成了 `tk.Text`，它默认把回车当"换行"。
          不吃掉这个事件的话，按一次回车 = 发送 + 在框里留一个空行，
          而且刚清空的内容会被那个换行符重新"垫"出来。
        """
        self._do_send()
        return "break"

    def _do_send(self) -> None:
        # `tk.Text` 的取法（Entry 是 `.get()`，Text 要带索引）
        text = self.entry.get("1.0", "end-1c").strip()
        if not text or self.busy:
            return
        # 上一条的语音还没合成好：先把它的文本交出来，别让回复凭空消失
        if self.pending_reply:
            pending = self.pending_reply
            self.pending_reply = None
            self._reveal_reply(pending["text"], pending.get("info") or {})
        self.entry.delete("1.0", "end")
        self._append("你", text, "user")
        self.busy = True
        self.send_button.configure(state="disabled")
        self.status_var.set("未央正在想…")
        try:
            if self.character:
                self.character.set_state("在想…", "warm")
        except Exception:
            pass
        lang = self._text_lang()
        threading.Thread(target=self._worker, args=(text, lang), daemon=True).start()

    def _worker(self, text: str, lang: str = "zh") -> None:
        try:
            reply, info = self.companion.say(
                text,
                tail_prompt=LANG_PROMPTS.get(lang, ""),
                validator=(lambda r: lang_matches(r, lang)) if lang in ("ja", "en") else None,
                retry_prompt=LANG_RETRY.get(lang, ""),
            )
            self.queue.put(("reply", text, reply, info))
        except Exception as exc:
            self.queue.put(("error", text, str(exc), {}))

    def _poll(self) -> None:
        # 无论中间出什么问题，都必须重新排上下一次——否则界面会永久卡死
        try:
            self._drain_queue()
        except queue.Empty:
            pass
        except Exception as exc:                      # 兜底：不让一次异常停掉轮询
            self.status_var.set(f"内部错误：{exc}"[:80])
        try:
            self._tick_speech()
        except Exception:
            pass
        self.root.after(120, self._poll)

    def _tick_speech(self) -> None:
        """播放状态跟踪：播完自动复位；卡住的播放器到时限就掐掉。"""
        if self._speech_state != "playing":
            return
        if not tts_engine.is_playing():
            self._speech_state = "idle"
            self._refresh_speech_button()
            self.status_var.set(self._status_line())
        elif time.time() - self._play_started > self.max_play_seconds:
            tts_engine.stop_playback()
            self._speech_state = "idle"
            self._refresh_speech_button()
            self.status_var.set(f"已按上限（{self.max_play_seconds:.0f} 秒）停止朗读")

    def _drain_queue(self) -> None:
        while True:
            kind, _text, payload, info = self.queue.get_nowait()
            if kind == "reply":
                self.busy = False
                self.send_button.configure(state="normal")
                speak = bool(self.autoplay_var.get()) and self._voice_cfg() is not None
                if speak:
                    # 先占位，等语音就绪再和声音一起显示正文
                    self.pending_reply = {"text": payload, "info": info}
                    self._show_pending()
                    self.status_var.set("未央已经想好了，正在合成语音…")
                    self._speak(payload, "朗读", reveal=True)
                else:
                    self._reveal_reply(payload, info)
                    self.status_var.set(self._status_line(info))
            elif kind == "error":
                self._append("系统", payload, "error")
                self.busy = False
                self.send_button.configure(state="normal")
                self.status_var.set("出错啦，看上面的红字")
            elif kind == "speech_ready":
                meta = info if isinstance(info, dict) else {}
                if meta.get("token") != self._speech_token:
                    continue        # 已被更新的请求取代，丢弃这次结果
                if meta.get("reveal") and self.pending_reply:
                    pending = self.pending_reply
                    self.pending_reply = None
                    self._reveal_reply(pending["text"], pending.get("info") or {})
                self.last_audio = payload
                proc = tts_engine.play_file(payload, stop_current=True)
                self._play_started = time.time()
                self._speech_state = "playing" if proc is not None else "idle"
                note = "（朗读用了译文）" if _text == "1" else ""
                self.status_var.set(f"正在朗读{note}　{pathlib.Path(payload).name}")
                self._refresh_speech_button()
            elif kind == "tts_err":
                meta = info if isinstance(info, dict) else {}
                if meta.get("reveal") and self.pending_reply:
                    # 语音失败也必须把文本交出来，不能吞掉回复
                    pending = self.pending_reply
                    self.pending_reply = None
                    self._reveal_reply(pending["text"], pending.get("info") or {})
                if meta.get("token") == self._speech_token:
                    self._speech_state = "idle"
                    self._refresh_speech_button()
                    self.status_var.set(f"语音失败：{payload[:60]}")
            elif kind == "svc_ok":
                if self.svc_win is not None and self.svc_win.winfo_exists():
                    self.svc_vars["status"].set(f"✓ {payload}")
                    self._svc_refresh()
                self.status_var.set(payload)
            elif kind == "svc_err":
                if self.svc_win is not None and self.svc_win.winfo_exists():
                    self.svc_vars["status"].set(f"✗ {payload}")
                self.status_var.set(f"服务启动失败：{payload[:80]}")

    def _replay_playing(self, link: str) -> bool:
        """这条「回听」是不是正在朗读（合成中或播放中都算）。"""
        return (getattr(self, "_speech_state", "idle") != "idle"
                and getattr(self, "_active_replay_link", None) == link)

    def _replay_reset_fg(self, link: str, normal_fg: str) -> str:
        """决定某条「回听」现在该是什么字色。

        两套效果**共存**，播放优先：
          · 正在朗读这条 → 隐形（字色 = 气泡底色，看着像没有）
          · 否则 → 常态可见（悬停时的高亮由 1 秒计时器负责）
        """
        if self._replay_playing(link):
            return getattr(self, "_replay_blank_fg", "#1A1F38")
        return normal_fg

    def _refresh_replay_playback(self) -> None:
        """播放状态变化时刷新「回听」：开始朗读 → 隐形；读完 → 按常态显出来。"""
        try:
            normal = self.chat.tag_cget("replay", "foreground") or "#E8ECFF"
        except Exception:
            normal = "#E8ECFF"
        for link in [x for x in self.chat.tag_names()
                     if x.startswith("replay") and x[6:].isdigit()]:
            try:
                self.chat.tag_configure(link,
                                        foreground=self._replay_reset_fg(link, normal))
            except Exception:
                pass

    def _restore_replay_fg(self, unique: str, normal_fg: str) -> None:
        """1 秒计时器到点：把这条「回听」的字色变回常态。

        如果它此刻**正在朗读**，就保持隐形（播放优先），等播放结束再由
        _refresh_replay_playback 显出来 —— 否则读到一半它会自己冒出来。
        """
        self._replay_timer = None
        try:
            self.chat.tag_configure(unique,
                                    foreground=self._replay_reset_fg(unique, normal_fg))
        except Exception:
            pass

    def _bind_replay_link(self, unique: str, index: int) -> None:
        """给某条「🔊回听」绑上：鼠标移上去字色变深蓝（1 秒后自动变回）+ 点击回听。

        只动**字色**，底色全程保持气泡色。
        先前试过"再加一个高亮标签、靠 tag_raise 盖上去"：标签优先级确实变了，
        但屏幕上的颜色没有跟着变；直接 tag_configure 这条链接自己的标签最稳，
        也不依赖优先级，多条回复之间天然互不影响。
        计时器只保留最新的一个：快速在多条之间移动时，不会出现旧的计时器
        把新点亮的那条提前变回去。
        Tk 的 tag_bind 只对鼠标下那段文字触发，所以不需要自己算命中范围。
        """
        hover_fg = self._replay_hover_fg
        try:
            # 常态字色直接读 replay 标签，保证「变回」时和常态完全一致
            normal_fg = self.chat.tag_cget("replay", "foreground") or "#E8ECFF"
        except Exception:
            normal_fg = "#E8ECFF"

        def enter(_event=None):
            try:
                self.chat.configure(cursor="hand2")
            except Exception:
                pass
            # 播放优先：正在朗读这条时保持隐形，悬停也不点亮（否则读到一半它会冒出来）
            if self._replay_playing(unique):
                try:
                    if self._replay_timer is not None:
                        self.root.after_cancel(self._replay_timer)
                except Exception:
                    pass
                self._replay_timer = None
                return "break"
            try:
                self.chat.tag_configure(unique, foreground=hover_fg)   # 变深蓝
            except Exception:
                pass
            # 1 秒后自动变回常态色（鼠标停着不动也会变回）
            try:
                if self._replay_timer is not None:
                    self.root.after_cancel(self._replay_timer)
            except Exception:
                pass
            try:
                self._replay_timer = self.root.after(
                    1000, lambda: self._restore_replay_fg(unique, normal_fg))
            except Exception:
                self._replay_timer = None
            return "break"

        def leave(_event=None):
            try:
                self.chat.configure(cursor="")
            except Exception:
                pass
            return "break"

        try:
            self.chat.tag_bind(unique, "<Enter>", enter)
            self.chat.tag_bind(unique, "<Leave>", leave)
            self.chat.tag_bind(unique, "<Button-1>",
                               lambda event, i=index: self._replay(i))
        except Exception:
            pass

    def _append(self, who: str, text: str, tag: str, index: int | None = None) -> None:
        if tag == "meta" and not getattr(self, "show_meta", True):
            return                                   # 设置里选了"隐藏内情行"
        if tag == "ai":
            # 回复到了 → 角色状态回到"在听你说"（角色区没起来时静默跳过）
            try:
                if self.character:
                    self.character.set_state("在听你说")
            except Exception:
                pass
        if tag == "meta":
            self.chat.insert("end", text, "meta")
        else:
            self.chat.insert("end", f"{who}：{text}", tag)
            if index is not None:
                # 每条回复后面挂一个可点的「回听」；用唯一 tag 绑定，避免都指向最后一条。
                # 前面那个空格也打上 ai 底色，让"未央：正文"和「回听」连成一条气泡，
                # 中间不留一道缝。
                unique = f"replay{index}"
                self.chat.insert("end", " ", "ai")
                self.chat.insert("end", "🔊回听", ("replay", unique))
                self._bind_replay_link(unique, index)
            self.chat.insert("end", "\n", tag)
        self.chat.see("end")

    # ---- 文本与声音同时出现 ----
    def _show_pending(self) -> None:
        """语音还在合成时，先放一条占位，避免"文本先出、声音没动静"被误判为失败。"""
        self._pending_line = int(self.chat.index("end-1c").split(".")[0])
        self.chat.insert("end", "未央：🔊 正在合成语音…", "pending")
        self.chat.insert("end", "\n")
        self.chat.see("end")

    def _reveal_reply(self, text: str, info: dict) -> None:
        """把回复正文放进对话区（若有占位就替换掉），并显示这轮的内情。"""
        self.replies.append(text)
        index = len(self.replies) - 1
        line = self._pending_line
        self._pending_line = None
        if line is not None:
            unique = f"replay{index}"
            self.chat.delete(f"{line}.0", f"{line}.end+1c")
            self.chat.insert(f"{line}.0", f"未央：{text}", "ai")
            # 用插入后的光标位置，回复本身含换行时也能把「回听」挂在末尾；
            # 空格同样用 ai 底色，保证跟气泡连成一块
            self.chat.insert(self.chat.index("insert"), " ", "ai")
            self.chat.insert(self.chat.index("insert"), "🔊回听", ("replay", unique))
            self.chat.insert(self.chat.index("insert"), "\n")
            self._bind_replay_link(unique, index)
            self.chat.see("end")
        else:
            self._append("未央", text, "ai", index=index)
        self._show_meta(info)
        # 两段式补充：**单独一条气泡连发**（2026-10-20）
        # ⚠ 为什么连发而不是并进同一条 —— 实测样例：
        #     主：嗯……这次是因为啥？
        #     补：你愿意说就说，不想说也没关系。吵完是该缓一缓的。
        #   并成一条读作「刚问完就退让」，别扭；**分成两条就像补了一句想法**，
        #   而且人本来就会连发几条。用户侧**看不到「补充」这个标签**，
        #   只看到未央多发了一条。
        supp = info.get("supplement") if isinstance(info, dict) else ""
        if supp:
            self.replies.append(supp)
            self._append("未央", supp, "ai", index=len(self.replies) - 1)
            self.chat.see("end")
        if getattr(self.companion, "last_retry", False):
            self._append("", "（语言没对上，已自动重生成一次）\n", "meta")

    def _show_meta(self, info: dict) -> None:
        parts = []
        level = self.companion.last_level
        if level:
            parts.append(f"{level.upper()} {LEVEL_NAMES.get(level, '')}")
        decision = self.companion.last_decision
        if decision is not None:
            parts.append(decision.brief())
        emotion = self.companion.last_emotion or {}
        if emotion.get("emotion") and emotion["emotion"] != "平静":
            parts.append(f"情绪 {emotion['emotion']}")
        events = self.companion.last_value_events or {}
        if events.get("new") or events.get("conflicts"):
            parts.append(f"价值观 {events.get('new') or events.get('conflicts')}")
        if info and info.get("summary"):
            parts.append(f"记忆 {info['summary']}")
        if parts:
            self._append("", "　·　".join(str(p) for p in parts) + "\n", "meta")
        self.status_var.set(self._status_line(info))

    def _status_line(self, info: dict | None = None) -> str:
        stats = self.companion.memory.stats()
        return (f"情景记忆 {stats['episodes']} 轮｜语义 {stats['semantic']} 条"
                f"｜价值观 {values_engine.stats(self.companion.store)['active']} 条生效")

    def _feedback(self, rating: str) -> None:
        if not self.companion.last_reply:
            self.status_var.set("还没有可评价的回复")
            return
        collector.record_last(self.companion.memory, rating)
        if rating == "up":
            values_engine.record_preference(
                self.companion.last_user, self.companion.last_reply, "", "up",
                path=self.companion.preferences_path)
        elif rating == "down":
            values_engine.record_preference(
                self.companion.last_user, "", self.companion.last_reply, "down",
                path=self.companion.preferences_path)
        self.status_var.set("已记为" + collector.RATINGS[rating])

    def _correct(self) -> None:
        if not self.companion.last_reply:
            self.status_var.set("还没有可纠正的回复")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("纠正上一轮")
        dialog.geometry("460x180")
        dialog.transient(self.root)
        style_dialog(dialog)
        ttk.Label(dialog, text="更希望它怎么回答？（会写成 DPO 偏好对）",
                  font=FONT_UI, padding=10).pack(anchor="w")
        box = tk.Text(dialog, height=4, font=FONT_UI, wrap="word")
        box.pack(fill="both", expand=True, padx=10)
        box.insert("1.0", self.companion.last_reply)

        def submit():
            note = box.get("1.0", "end").strip()
            dialog.destroy()
            if not note:
                return
            collector.record_last(self.companion.memory, "correction", note)
            values_engine.record_preference(
                self.companion.last_user, note, self.companion.last_reply, "correction",
                path=self.companion.preferences_path)
            signals = values_engine.detect_signals(
                note, feedback={"rating": "correction", "note": note})
            proposal = values_engine.propose_candidates(
                self.companion.generator, self.companion.last_user, note, signals,
                self.companion.store)
            values_engine.consolidate(self.companion.store, proposal, signals,
                                      self.companion.turn, self.companion.last_user)
            values_engine.save_store(self.companion.store, self.companion.values_path)
            self.status_var.set("已记下纠正（偏好对 + 价值观信号）")

        ttk.Button(dialog, text="保存", command=submit).pack(pady=8)
        center_window(dialog, 460, 180)

    def _reset(self) -> None:
        self.companion.reset_context()
        self.replies.clear()
        self.pending_reply = None
        self._pending_line = None
        tts_engine.stop_playback()
        self._speech_token += 1
        self._speech_state = "idle"
        self._refresh_speech_button()
        self.chat.delete("1.0", "end")
        self._append("未央", "好，我们从头聊。", "ai")
        self.status_var.set("短期上下文已清空（长期记忆保留）")

    def _show_text(self, title: str, body: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("620x420")
        style_dialog(dialog)
        try:
            from gui_theme import COLORS as _C4
        except Exception:
            _C4 = {}
        box = scrolledtext.ScrolledText(
            dialog, wrap="word", font=FONT_UI, relief="flat",
            background=_C4.get("bg", "#0d1220"),
            foreground=_C4.get("text", "#E8ECFF"),
            insertbackground=_C4.get("text", "#E8ECFF"))
        theme_scroll(box, _C4)
        box.pack(fill="both", expand=True)
        box.insert("1.0", body or "（空）")
        box.configure(state="disabled")
        theme_scroll(box, _C4)          # 内建滚动条要 pack 之后才存在，这里再扫一次
        center_window(dialog, 620, 420)

    def _check_services(self) -> None:
        from cognition.vectors import health as embedding_health
        from engine.generate import Generator  # noqa: F401
        notes = []
        if not embedding_health():
            notes.append("嵌入服务未启动（8081）→ 检索退化为关键词匹配")
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3):
                pass
        except Exception:
            notes.append("对话模型未启动（8080）→ 先运行 python 启动模型.py")
        if notes:
            self._append("系统", "；".join(notes), "error")


def main() -> int:
    # 启动时自动拉起嵌入服务（8081）：不拉的话记忆检索一直退化在纯词法（BM25），
    # 而且写入的记忆不会被向量化（没有回填机制，索引会静默落后）。
    # 放**入口**而不是 Companion.__init__：测试会构造 Companion，
    # 放类里会到处拉起 llama-server，也会和自带起停嵌入服务的基准测试冲突。
    # 起不来只打印一行——嵌入是可选能力，不该挡住开窗。
    # ⚠ **开关**（`config/models.yaml: switches.autostart`，界面里可勾）：
    #   关掉时**一个都不自动拉**，用户自己起（基准测试/只想聊天时有用）。
    _autostart_on = True
    try:
        from services import models_config as _mc
        _autostart_on = bool(_mc.switch("autostart"))
    except Exception:
        _autostart_on = True

    if _autostart_on:
        try:
            from services.autostart import ensure_embed_service, report as report_embed
            report_embed(ensure_embed_service())
        except Exception as exc:        # 兜底：自动拉起绝不能挡住开窗
            print(f"[记忆检索] 自动拉起嵌入服务时出错（忽略）：{type(exc).__name__}: {exc}")

    # 自动拉起**记忆抽取器**（8083 的 Qwen3.5-2B）。为什么与聊天分开：
    # 抽取和聊天共权重会被微调带偏（实测「林小雨」→「林小记得」）。
    # 起不来也没关系——抽取会**逐次回落到聊天模型**，记忆照常落库。
    if _autostart_on:
        try:
            from services.autostart import (ensure_extract_service,
                                            report as report_extract)
            report_extract(ensure_extract_service(), label="记忆抽取")
        except Exception as exc:
            print(f"[记忆抽取] 自动拉起抽取器时出错（忽略）：{type(exc).__name__}: {exc}")

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    window = Window(root)
    # 索引回填：把「库里有、索引里没有」的记忆补上向量。
    # 起因：`cognition/extract.py:297` 把 add_texts 的异常全吞掉（合理），
    # 但**没有回填机制** → 嵌入服务关闭期间写入的记忆永远不会被向量化
    # （实测：库里 8 条、索引只有 3 条）。走后台线程，不拖慢开窗。
    try:
        comp = getattr(window, "companion", None)
        if comp is not None:
            comp.backfill_index()
    except Exception as exc:
        print(f"[记忆索引] 回填时出错（忽略）：{type(exc).__name__}: {exc}")
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
