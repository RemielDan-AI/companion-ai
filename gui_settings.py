"""设置窗口：左侧分类 + 右侧设置项（参考 Cyrene-Agent 的设置界面布局）。

对齐参考项目的几件事：
  · **独立窗口**，打开时**居中到屏幕**
  · **左侧分类 + 右侧内容**：语音 / 语言 / 对话框 / 字体 / 模型服务 / 记忆与数据 / 关于
  · **圆角**：按钮与选中项用 Canvas 自绘圆角（Tkinter 没有原生圆角）
  · 深色鲸蓝配色，与主界面同源（gui_theme.COLORS）
  · 改动即时生效：控件绑定回调，由调用方决定怎么写回配置

独立可用：拿不到 gui_theme 时用内置兜底色，主题模块出问题也能打开设置。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from services import models_config

try:
    from gui_theme import COLORS, FONT_SMALL, FONT_TITLE, FONT_UI, pick_font, round_rect
except Exception:                                    # 兜底：保证窗口仍可用
    COLORS = {"bg": "#0F1220", "panel": "#161A2E", "panel_hi": "#1F2544",
              "accent": "#4D6BFE", "accent_soft": "#6B8AFF", "text": "#E8ECFF",
              "text_dim": "#9AA6CC", "ok": "#57D1A0", "warn": "#FFCC66",
              "err": "#FF7A7A"}
    FONT_UI = FONT_TITLE = ("Microsoft YaHei UI", 10)
    FONT_SMALL = ("Microsoft YaHei UI", 9)

    def pick_font(root):
        return None

    def round_rect(canvas, x1, y1, x2, y2, radius=10, **kwargs):
        return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)


CATEGORIES = ("语音", "语言", "对话框", "字体", "模型服务", "记忆与数据", "关于")

EDGE_VOICES = (
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunyangNeural", "zh-CN-XiaobeiNeural",
    "ja-JP-NanamiNeural", "ja-JP-KeitaNeural",
    "en-US-AriaNeural", "en-US-GuyNeural",
)

FONT_CHOICES = ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun",
                "KaiTi", "Noto Sans CJK SC", "Source Han Sans SC",
                "Consolas", "Segoe UI", "Arial")
FONT_SIZES = ("10", "11", "12", "13", "14", "15", "16", "18", "20")


# 名字里带这些词的字体一般是符号/图标字体，选了也显示不出中文，直接排除
SYMBOL_HINTS = ("symbol", "wingdings", "webdings", "marlett", "mdl2", "hololens",
                "emoji", "awesome", "icon", "icons", "dingbat", "material",
                "monotype sorts", "segoe ui historic", "segoe ui symbol",
                "cambria math", "bookshelf symbol")
# 中文常用字体优先排前面（不是"只有这些"，是把好用的放上面）
CJK_HINTS = ("雅黑", "yahei", "黑体", "simhei", "宋体", "simsun", "楷体", "kaiti",
             "仿宋", "fangsong", "等线", "dengxian", "思源", "source han", "noto",
             "文泉驿", "wenquanyi", "微软", "microsoft", "苹方", "pingfang",
             "幼圆", "youyuan", "隶书", "liSu", "圆体", "兰亭", "方正")


def system_fonts(widget) -> list[str]:
    """本机已装字体（与系统同步），中文优先排前。

    排除：@ 开头的竖排变体、明显的符号/图标字体、空名。
    """
    try:
        names = {str(name).strip() for name in tkfont.families(widget)}
    except Exception:
        return list(FONT_CHOICES)
    cleaned = []
    for name in names:
        if not name or name.startswith("@"):
            continue
        lowered = name.lower()
        if any(hint in lowered for hint in SYMBOL_HINTS):
            continue
        cleaned.append(name)
    preferred = sorted([n for n in cleaned
                        if any(hint.lower() in n.lower() for hint in CJK_HINTS)])
    others = sorted([n for n in cleaned if n not in preferred])
    ordered = preferred + others
    return ordered or list(FONT_CHOICES)


class RoundedButton(tk.Canvas):
    """圆角按钮（Canvas 自绘）——Tkinter 的按钮是方角的，这里补上圆角观感。"""

    def __init__(self, master, text: str, command=None, width: int = 96,
                 height: int = 30, fill: str | None = None, fg: str | None = None,
                 **kw):
        super().__init__(master, width=width, height=height, bg=master["bg"],
                         highlightthickness=0, **kw)
        self._text = text
        self._command = command
        self._fill = fill or COLORS["panel_hi"]
        self._fg = fg or COLORS["text"]
        # 注意：**不要**用 self._w / self._h —— _w 是 tkinter 内部保存控件
        # Tcl 路径名的保留属性，覆盖它会让后续所有 tk.call 去找一个叫 "80"
        # 的控件（报 invalid command name "80"）。这里用 _btn_w / _btn_h。
        self._btn_w, self._btn_h = width, height
        self._draw(False)
        self.bind("<Enter>", lambda _e: self._draw(True))
        self.bind("<Leave>", lambda _e: self._draw(False))
        self.bind("<Button-1>", self._click)

    def _draw(self, hover: bool) -> None:
        self.delete("all")
        round_rect(self, 1, 1, self._btn_w - 1, self._btn_h - 1,
                   radius=self._btn_h // 2,
                   fill=COLORS["accent"] if hover else self._fill, outline="")
        self.create_text(self._btn_w // 2, self._btn_h // 2 + 1, text=self._text,
                         fill=self._fg, font=FONT_UI)

    def _click(self, _event=None) -> None:
        if callable(self._command):
            self._command()


class SettingsWindow(tk.Toplevel):
    """分栏式设置窗口（打开即居中）。"""

    def __init__(self, master, *, ui_state: dict | None = None,
                 voice_config: dict | None = None, companion=None,
                 manager=None, on_change=None, on_wipe=None):
        super().__init__(master)
        self.title("设置 · 未央")
        self.configure(bg=COLORS["bg"])
        # ⚠ 2026-10-24：原来是 760x480，**明显不够**——
        #   「模型服务」一页光角色表（5 行 × 模型下拉+端口+ngl+ctx+常驻+状态）
        #   就要 400px 以上，再加五个开关和按钮，760x480 必然把大半截切掉。
        #   现在默认 1000x740，而且内容区**可滚动**（见下面的 canvas），
        #   所以窗口再小也不会有"看不到的设置"。
        self._want_size = (1000, 740)
        self.geometry("1000x740")
        self.minsize(760, 460)
        self.transient(master)
        self.ui_state = ui_state if ui_state is not None else {}

        # ---- 字号（2026-10-27 新增）----
        # ⚠ 必须**在建设置页之前**把字体换成新字号：
        #   Tk 的控件是建的时候就把 `font=` 固化下来的，建完再改主题变量不会生效。
        # ⚠ 还要 `sync_fonts(globals())`：`from gui_theme import FONT_UI` 拿到的是
        #   **当时的元组副本**，改 `gui_theme.FONT_UI` 不会更新本模块手里的那个（踩过）。
        try:
            _size = int(self.ui_state.get("settings_font_size") or 9)
        except Exception:
            _size = 9
        try:
            import gui_theme as _gt
            _gt.pick_font(self, base=_size)
            _gt.sync_fonts(globals())
        except Exception:
            try:
                pick_font(self)
            except Exception:
                pass
        self.voice_config = voice_config or {}
        self.companion = companion
        self.manager = manager
        self.on_change = on_change
        self.on_wipe = on_wipe
        self.vars: dict[str, tk.Variable] = {}
        self._panels: dict[str, tk.Frame] = {}
        self._current = CATEGORIES[0]

        body = tk.Frame(self, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)
        self._build_sidebar(body)

        # ---- 可滚动的内容区 ----
        # ⚠ 为什么要滚：设置项只会越来越多（模型五个角色、开关、字体、语音…），
        #   固定窗口高度迟早有东西被切掉。**切掉的设置等于不存在**——
        #   用户根本不知道下面还有。所以内容区一律包一层 Canvas。
        wrap = tk.Frame(body, bg=COLORS["bg"])
        wrap.pack(side="left", fill="both", expand=True, padx=(0, 6), pady=10)
        self._scroll_canvas = tk.Canvas(wrap, bg=COLORS["bg"],
                                        highlightthickness=0, bd=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical",
                             command=self._scroll_canvas.yview)
        self._scroll_canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        self._scroll_canvas.pack(side="left", fill="both", expand=True)

        self._content = tk.Frame(self._scroll_canvas, bg=COLORS["bg"])
        self._content_id = self._scroll_canvas.create_window(
            (0, 0), window=self._content, anchor="nw")
        # 内容变大 → 更新滚动范围；画布变宽 → 让内容跟着铺满
        self._content.bind(
            "<Configure>",
            lambda _e: self._scroll_canvas.configure(
                scrollregion=self._scroll_canvas.bbox("all")))
        self._scroll_canvas.bind(
            "<Configure>",
            lambda e: self._scroll_canvas.itemconfigure(self._content_id, width=e.width))
        # 鼠标滚轮（只在这个窗口里有效）
        self._scroll_canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")

        for name in CATEGORIES:
            self._panels[name] = tk.Frame(self._content, bg=COLORS["bg"])
        self._build_panels()
        self.show(self._current)

        footer = tk.Frame(self, bg=COLORS["bg"])
        footer.pack(fill="x", pady=(0, 12))
        RoundedButton(footer, "关闭", command=self.destroy).pack(side="right", padx=(0, 16))

        self._center()
        self.bind("<Escape>", lambda _e: self.destroy())


    def _on_wheel(self, event) -> None:
        """滚轮滚动内容区。⚠ 只在**本窗口存在**时响应，关掉要解绑。"""
        try:
            if not self.winfo_exists():
                return
            self._scroll_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def destroy(self) -> None:
        """关窗口时**解绑全局滚轮**——否则滚轮会继续往一个不存在的画布上打。"""
        try:
            self.unbind_all("<MouseWheel>")
        except Exception:
            pass
        super().destroy()

    # ---------------- 居中 ----------------
    def _center(self) -> None:
        """把窗口放到屏幕正中。

        ⚠⚠ 2026-10-27 修了一个"窗口开成最小尺寸"的 bug：

        原来先 `update_idletasks()` 再取 `self.winfo_width()`。但此时窗口
        **还没映射**，`winfo_width()` 返回的是**自然请求尺寸**（约 768×497），
        于是紧接着的 `self.geometry(f"{width}x{height}+{x}+{y}")`
        **把 `__init__` 里设的 `1000x740` 覆盖掉了**。

        后果：设置窗口总是开得很小 → 「模型」那一列被挤成一条缝，
        模型名字根本看不见（用户截图就是这样）。

        → 改成用 `__init__` 里**记下的目标尺寸**定位，不读未映射时的实际尺寸。
        """
        try:
            width, height = getattr(self, "_want_size", (1000, 740))
            self.update_idletasks()
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
            x = max(0, (screen_w - width) // 2)
            y = max(0, (screen_h - height) // 3)      # 略偏上，视觉更稳
            self.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            pass

    # ---------------- 左侧分类 ----------------
    def _build_sidebar(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=COLORS["panel"], width=150)
        bar.pack(side="left", fill="y")
        bar.pack_propagate(False)
        tk.Label(bar, text="设置", bg=COLORS["panel"], fg=COLORS["text"],
                 font=FONT_TITLE).pack(anchor="w", padx=14, pady=(16, 10))
        self._tab_canvases: dict[str, tk.Canvas] = {}
        for name in CATEGORIES:
            canvas = tk.Canvas(bar, width=126, height=30, bg=COLORS["panel"],
                               highlightthickness=0, cursor="hand2")
            canvas.pack(padx=12, pady=1)
            self._tab_canvases[name] = canvas
            canvas.bind("<Button-1>", lambda _e, n=name: self.show(n))
            canvas.bind("<Enter>", lambda _e, n=name: self._paint_tab(n, hover=True))
            canvas.bind("<Leave>", lambda _e, n=name: self._paint_tab(n, hover=False))
        self._paint_all_tabs()

    def _paint_tab(self, name: str, hover: bool = False) -> None:
        active = name == self._current
        canvas = self._tab_canvases[name]
        canvas.delete("all")
        if active or hover:
            round_rect(canvas, 2, 2, 124, 28, radius=14,
                       fill=COLORS["accent"] if active else COLORS["panel_hi"],
                       outline="")
        canvas.create_text(16, 15, text=name, anchor="w",
                           fill=COLORS["text"] if active else COLORS["text_dim"],
                           font=FONT_UI)

    def _paint_all_tabs(self) -> None:
        for name in CATEGORIES:
            self._paint_tab(name)

    def show(self, name: str) -> None:
        self._current = name
        self._paint_all_tabs()
        for key, panel in self._panels.items():
            panel.pack_forget()
        self._panels[name].pack(fill="both", expand=True)
        # 切页后**回到顶部**：否则从"模型服务"（很长）切到"语音"（很短）时，
        # 滚动位置还停在下面，新页面看起来是空的。
        try:
            self._scroll_canvas.yview_moveto(0)
        except Exception:
            pass

    # ---------------- 通用控件 ----------------
    def _title(self, parent: tk.Frame, text: str, hint: str = "") -> None:
        tk.Label(parent, text=text, bg=COLORS["bg"], fg=COLORS["text"],
                 font=FONT_TITLE).pack(anchor="w", pady=(0, 2))
        if hint:
            tk.Label(parent, text=hint, bg=COLORS["bg"], fg=COLORS["text_dim"],
                     font=FONT_SMALL, justify="left", wraplength=520).pack(
                anchor="w", pady=(0, 10))

    def _row(self, parent: tk.Frame, label: str) -> tk.Frame:
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(fill="x", pady=5)
        tk.Label(row, text=label, bg=COLORS["bg"], fg=COLORS["text_dim"],
                 font=FONT_UI, width=12, anchor="w").pack(side="left")
        return row

    def _combo(self, parent: tk.Frame, key: str, values, default: str = "",
               width: int = 26, height: int = 12) -> ttk.Combobox:
        var = tk.StringVar(value=str(self.ui_state.get(key, default)))
        self.vars[key] = var
        box = ttk.Combobox(parent, textvariable=var, values=list(values), width=width,
                           state="readonly", font=FONT_UI, height=height)
        box.pack(side="left", padx=(6, 10))
        box.bind("<<ComboboxSelected>>", lambda _e: self._changed(key, var.get()))
        return box

    def _check(self, parent: tk.Frame, key: str, text: str,
               default: bool = False) -> None:
        var = tk.BooleanVar(value=bool(self.ui_state.get(key, default)))
        self.vars[key] = var
        tk.Checkbutton(parent, text=text, variable=var, bg=COLORS["bg"],
                       fg=COLORS["text"], activebackground=COLORS["bg"],
                       activeforeground=COLORS["text"], selectcolor=COLORS["accent"],
                       font=FONT_UI, highlightthickness=0,
                       command=lambda: self._changed(key, var.get())).pack(anchor="w",
                                                                          pady=3)

    def _box(self, parent: tk.Frame, height: int = 6) -> tk.Text:
        box = tk.Text(parent, height=height, bg=COLORS["panel_hi"], fg=COLORS["text"],
                      font=FONT_SMALL, relief="flat", wrap="word", padx=10, pady=8)
        box.pack(fill="x", pady=(0, 8))
        return box

    # ---------------- 各页 ----------------
    def _build_panels(self) -> None:
        # 语音
        page = self._panels["语音"]
        self._title(page, "语音", "选择朗读方式；本地语音需要先启动对应服务")
        self._combo(self._row(page, "朗读方式"), "backend",
                    ["不语音", "Edge TTS", "本地 GSV", "OpenAI 兼容"], "不语音")
        self._combo(self._row(page, "Edge 声音"), "edge_voice", EDGE_VOICES, "")
        self._check(self._row(page, "自动朗读"), "auto_speak", "回复后自动朗读", True)
        row = self._row(page, "服务地址")
        var = tk.StringVar(value=str(self.ui_state.get("tts_url", "")))
        self.vars["tts_url"] = var
        entry = ttk.Entry(row, textvariable=var, width=30, font=FONT_UI)
        entry.pack(side="left", padx=(6, 10))
        entry.bind("<FocusOut>", lambda _e: self._changed("tts_url", var.get()))

        # 语言
        page = self._panels["语言"]
        self._title(page, "语言", "「文本语言」决定未央用什么语言写；"
                                  "「朗读语言」决定念出来是什么语言，两者可以不同。")
        self._combo(self._row(page, "文本语言"), "text_lang",
                    ["中文", "日文", "英文"], "中文")
        self._combo(self._row(page, "朗读语言"), "speak_lang",
                    ["自动", "中文", "日文", "英文"], "自动")
        tk.Label(page, text="选了「自动」就跟随文本语言；不一致时只翻译语音，"
                            "聊天记录保持原文。", bg=COLORS["bg"],
                 fg=COLORS["text_dim"], font=FONT_SMALL, justify="left",
                 wraplength=520).pack(anchor="w", pady=(6, 0))

        # 对话框
        page = self._panels["对话框"]
        self._title(page, "对话框", "聊天区显示什么、什么行为；改完立即生效")
        self._combo(self._row(page, "聊天字号"), "chat_font_size",
                    FONT_SIZES, str(self.ui_state.get("chat_font_size", "12")))
        self._combo(self._row(page, "内情行"), "show_meta", ["显示", "隐藏"], "显示")
        self._combo(self._row(page, "回听链接"), "show_replay", ["显示", "隐藏"], "显示")
        self._combo(self._row(page, "记忆注入"), "inject_memory", ["开启", "关闭"], "开启")
        tk.Label(page, text="「内情行」指每条回复后面那行小字（档位/技能/情绪/记忆）；"
                            "「记忆注入」关掉后未央不再使用长期记忆。",
                 bg=COLORS["bg"], fg=COLORS["text_dim"], font=FONT_SMALL,
                 justify="left", wraplength=520).pack(anchor="w", pady=(6, 0))

        # 字体
        page = self._panels["字体"]
        self._title(page, "字体", "列表与本机已装字体同步；中文建议雅黑/思源/黑体")
        families = system_fonts(self)
        preferred = str(self.ui_state.get("ui_font_family", "Microsoft YaHei UI"))
        if preferred not in families:
            families.insert(0, preferred)
        hint = (f"本机共 {len(families)} 个字体可选"
                f"（已排除 @ 开头的竖排变体和纯符号字体）")
        tk.Label(page, text=hint, bg=COLORS["bg"], fg=COLORS["text_dim"],
                 font=FONT_SMALL).pack(anchor="w", pady=(0, 8))
        self._combo(self._row(page, "界面字体"), "ui_font_family", families,
                    preferred, width=30, height=24)
        self._combo(self._row(page, "界面字号"), "ui_font_size", FONT_SIZES,
                    str(self.ui_state.get("ui_font_size", "10")), height=18)
        # 设置窗口自己的字号（改完**重开设置窗口**生效——Tk 控件建完就固定字体）
        self._combo(self._row(page, "设置窗口字号"), "settings_font_size",
                    [str(n) for n in range(8, 15)],
                    str(self.ui_state.get("settings_font_size", "9")), height=14)
        self._combo(self._row(page, "聊天字体"), "chat_font_family", families,
                    str(self.ui_state.get("chat_font_family", preferred)), width=30,
                    height=24)
        tk.Label(page, text="「界面字号」影响主窗口；「设置窗口字号」只影响本窗口"
                            "（**重开设置窗口生效**）；「聊天字体」只影响聊天区。"
                            "下拉列表可用滚轮翻；改完不满意随时改回来。",
                 bg=COLORS["bg"], fg=COLORS["text_dim"], font=FONT_SMALL,
                 justify="left", wraplength=520).pack(anchor="w", pady=(6, 0))

        # 模型服务
        page = self._panels["模型服务"]
        self._title(page, "模型服务",
                    "五个角色各用哪个模型由你定；保存后「一键拉起」按这份配置起服务")
        self._build_model_roles(page)
        self.service_box = self._box(page, 6)
        RoundedButton(page, "刷新服务状态", command=self._refresh_model_roles,
                      width=110).pack(anchor="w", pady=(6, 0))
        self._refresh_model_roles()

        # 记忆与数据
        page = self._panels["记忆与数据"]
        self._title(page, "记忆与数据", "记忆保存在本机；清除前会再确认一次，"
                                        "默认保留价值观")
        self.memory_box = self._box(page, 6)
        holder = tk.Frame(page, bg=COLORS["bg"])
        holder.pack(anchor="w")
        RoundedButton(holder, "刷新统计", command=self._refresh_memory,
                      width=96).pack(side="left")
        RoundedButton(holder, "清除长期数据…", command=self._wipe, width=126,
                      fg=COLORS["err"]).pack(side="left", padx=10)
        self._refresh_memory()

        # 关于
        page = self._panels["关于"]
        self._title(page, "关于", "本地运行的专属陪伴 AI")
        for line in ("未央 · 陪伴 AI —— 记忆优先、风格优先、本地优先、轻量优先。", "",
                     "· 记忆与画像都存在本机，云端默认关闭（外发有白名单与审计）",
                     "· 技能白名单：没技能就诚实拒绝，拒绝走本地模板、不经过模型",
                     "· 界面为自绘矢量形象与深色主题；立绘素材按路径引用本地皮肤包"):
            tk.Label(page, text=line, bg=COLORS["bg"], fg=COLORS["text_dim"],
                     font=FONT_SMALL, justify="left", wraplength=520).pack(
                anchor="w", pady=1)


    # ---------------- 五个角色的模型设置（2026-10-21） ----------------
    def _build_model_roles(self, page) -> None:
        """每个角色一行：**模型 / 端口 / ngl / ctx / 常驻**。

        ## 三个设计点

        ⚠ **① 同一个模型可以服务多个角色**（防呆，`models_config.shared_with`）。
          判官用 9B 最准，用户很可能把判官也设成 9B——那**只该起一份**。
          这里不拦着用户这么配，只在运行时复用；状态那栏会显示「共用 X」。

        ⚠ **② 常驻勾选**：勾了的角色会被「一键拉起」拉起来。
          默认只勾 对话/嵌入/记忆。**思考模型常驻要在意内存**：
          三个常驻 + 9B 只剩 0.44 GB（实测），勾之前先看下面的提示。

        ⚠ **③ 启动参数（端口/ngl/ctx）**：ngl 是最要紧的那个——
          `memory` 的 **ngl=12 是实测平衡点**（纯 CPU 反而慢 5 倍，
          中间值 20 比两端都差：层在 GPU/CPU 来回搬的开销大于它省下的计算）。
          改大 ngl 会抢显存，把对话模型挤到 CPU 上，整体更慢。
        """
        from services.manager import discover_models

        models = discover_models()
        chat_models = [m["file"] for m in models if m["kind"] == "chat"]
        embed_models = [m["file"] for m in models if m["kind"] == "embed"]
        self._role_vars: dict[str, tk.StringVar] = {}
        self._role_hint = {}
        self._param_vars: dict[str, dict[str, tk.StringVar]] = {}
        self._resident_vars: dict[str, tk.BooleanVar] = {}
        self._source_vars: dict[str, tk.StringVar] = {}
        self._api_vars: dict[str, dict[str, tk.StringVar]] = {}
        self._api_holders: dict[str, tk.Frame] = {}
        self._local_holders: dict[str, tk.Frame] = {}
        self._model_boxes: dict[str, ttk.Combobox] = {}
        self._fields_frames: dict[str, tk.Frame] = {}
        self._param_widgets: dict[str, dict[str, tk.Entry]] = {}

        cfg = models_config.load()
        # =====================================================================
        # ⚠ 2026-10-27：这张表从 **pack 改成 grid**。
        #
        # 原来每行是 `Label(width=8) + Combobox(width=9) + Combobox(width=28)`
        # 一路 `side="left"` 排下去 —— 但 **`width` 的单位不一样**：
        #   · `tk.Label(width=8)`      = 8 个**字符宽**（按该控件字体算）
        #   · `ttk.Combobox(width=9)`  = 9 个字符宽 **再加下拉箭头和边框**
        # 所以列和列永远对不齐（用户截图里「端口 / ngl / ctx」明显错位）。
        #
        # grid 才是表格该用的工具：**列宽由整列最宽的那个决定**，
        # 每行同一列落在同一条竖直线上，跟控件类型无关。
        # =====================================================================
        holder = tk.Frame(page, bg=COLORS["bg"])
        holder.pack(fill="x", anchor="w", pady=(0, 4))

        COLS = (
            ("角色", 92), ("来源", 110), ("模型 / API 地址", 0), ("端口", 62),
            ("ngl", 44), ("ctx", 62), ("常驻", 46), ("状态", 130),
        )
        for col, (text, minsize) in enumerate(COLS):
            tk.Label(holder, text=text, anchor="w", bg=COLORS["bg"],
                     fg=COLORS["text_dim"], font=FONT_SMALL).grid(
                row=0, column=col, sticky="w", padx=(0, 6), pady=(0, 4))
            if minsize:
                holder.grid_columnconfigure(col, minsize=minsize)
        # ⚠ 光有 weight 不够：窗口一窄，这一列会被**压到几乎为 0**
        #   （下拉框只剩一个小方块，模型名字看不见）。必须给 minsize 兜底。
        holder.grid_columnconfigure(2, weight=1, minsize=250)

        self._param_widgets: dict[str, dict[str, tk.Entry]] = {}

        for index, role in enumerate(models_config.ROLES, start=1):
            item = cfg["roles"].get(role) or {}
            choices = embed_models if role == "embed" else chat_models
            cur = str(item.get("model") or "")
            if cur and cur not in choices:
                choices = [cur] + list(choices)

            # ① 角色名
            tk.Label(holder, text=models_config.ROLE_LABELS[role], anchor="w",
                     bg=COLORS["bg"], fg=COLORS["text"], font=FONT_UI).grid(
                row=index, column=0, sticky="w", padx=(0, 6), pady=1)

            # ② 来源：本地模型 / 远程 API
            # ⚠ 走 API 意味着**对话内容要发到外面**，必须过 `cloud/gateway.py`
            #   外发网关（默认 enabled: false，一律拒绝）。界面如实显示
            #   "配了但发不出去"，而不是假装可用。
            src_var = tk.StringVar(
                value=models_config.SOURCE_LABELS.get(models_config.source(role),
                                                      "本地模型"))
            self._source_vars[role] = src_var
            src_box = ttk.Combobox(holder, textvariable=src_var, width=8,
                                   values=[models_config.SOURCE_LABELS[s]
                                           for s in models_config.SOURCES],
                                   state="readonly", font=FONT_UI)
            src_box.grid(row=index, column=1, sticky="w", padx=(0, 6), pady=1)
            src_box.bind("<<ComboboxSelected>>",
                         lambda _e, r=role: self._toggle_source(r))

            # ③ 模型下拉（本地用）
            var = tk.StringVar(value=cur or (choices[0] if choices else ""))
            self._role_vars[role] = var
            model_box = ttk.Combobox(holder, textvariable=var, values=choices,
                                     width=26, state="readonly", font=FONT_UI)
            model_box.grid(row=index, column=2, sticky="ew", padx=(0, 6), pady=1)
            self._model_boxes[role] = model_box

            # ④ API 三个输入框：跨 2~5 列，顶掉「模型 + 端口 + ngl + ctx」
            api_holder = tk.Frame(holder, bg=COLORS["bg"])
            av: dict[str, tk.StringVar] = {}
            for key, width in (("api_base", 24), ("api_model", 12),
                               ("api_key_env", 15)):
                iv = tk.StringVar(value=str(item.get(key) or ""))
                av[key] = iv
                tk.Entry(api_holder, textvariable=iv, width=width,
                         font=FONT_UI).pack(side="left", padx=(0, 4))
            self._api_vars[role] = av
            self._api_holders[role] = api_holder
            api_holder.grid(row=index, column=2, columnspan=4, sticky="w",
                            padx=(0, 6), pady=1)
            api_holder.grid_remove()

            # ⑤ 启动参数（端口 / ngl / ctx）：只对本地模型有意义
            pv: dict[str, tk.StringVar] = {}
            pw: dict[str, tk.Entry] = {}
            for col, (key, width) in enumerate((("port", 5), ("ngl", 4),
                                                ("ctx", 5)), start=3):
                # ⚠ 不能用 `str(item.get(key) or "")`：**ngl=0 是合法值**
                #   （嵌入/判官走 CPU），`0 or ""` 会得到空字符串，
                #   看起来像"没配置"，保存时还会被跳过。
                _v = item.get(key)
                pvar = tk.StringVar(value="" if _v is None else str(_v))
                pv[key] = pvar
                e = tk.Entry(holder, textvariable=pvar, width=width, font=FONT_UI)
                e.grid(row=index, column=col, sticky="w", padx=(0, 6), pady=1)
                pw[key] = e
            self._param_vars[role] = pv
            self._param_widgets[role] = pw

            # ⑥ 常驻
            rvar = tk.BooleanVar(value=bool(item.get("resident")))
            self._resident_vars[role] = rvar
            tk.Checkbutton(holder, variable=rvar, bg=COLORS["bg"],
                           activebackground=COLORS["bg"],
                           selectcolor=COLORS["accent"],
                           highlightthickness=0, bd=0).grid(
                row=index, column=6, sticky="w", padx=(0, 6), pady=1)

            # ⑦ 状态：初始写「探测中」而不是空白（异步探活 ~0.4 秒）
            state = tk.Label(holder, text="⋯ 探测中", anchor="w",
                             bg=COLORS["bg"], fg=COLORS["text_dim"],
                             font=FONT_SMALL)
            state.grid(row=index, column=7, sticky="w", pady=1)
            self._role_hint[role] = state

            self._toggle_source(role)      # 按当前来源决定显示哪一组字段

        btns = tk.Frame(page, bg=COLORS["bg"])
        btns.pack(anchor="w", pady=(8, 2))
        RoundedButton(btns, "保存角色设置", command=self._save_roles, width=120).pack(
            side="left")
        RoundedButton(btns, "一键拉起", command=self._start_all_models, width=96).pack(
            side="left", padx=8)
        RoundedButton(btns, "停掉全部", command=self._stop_all_models, width=88).pack(
            side="left")
        # 用户要求：「固定回复用一个文件维护，可以在设置里打开这个文件」
        RoundedButton(btns, "打开固定回复文件", command=self._open_replies,
                      width=140).pack(side="left", padx=(8, 0))
        # 思考模型单独一个按钮：按 `exclusive_think` 会**先关判官**再拉起
        RoundedButton(btns, "拉起思考模型", command=self._start_think,
                      width=110).pack(side="left", padx=8)

        # ---- 五个开关（2026-10-21）----
        sw_holder = tk.Frame(page, bg=COLORS["bg"])
        sw_holder.pack(anchor="w", fill="x", pady=(6, 2))
        self._switch_vars: dict[str, tk.BooleanVar] = {}
        SWITCHES = (
            ("autostart", "启动界面时自动拉起常驻模型"),
            ("opportunistic_judge", "机会主义判官（有思考模型就用它判）"),
            ("exclusive_think", "拉起思考模型前先关判官模型（腾内存）"),
            ("stop_on_exit", "关闭界面时停掉本次拉起的模型"),
            ("auto_think", "难题时自动拉起思考模型（起要 10~15 秒）"),
        )
        for key, label in SWITCHES:
            var = tk.BooleanVar(value=models_config.switch(key))
            self._switch_vars[key] = var
            tk.Checkbutton(sw_holder, text=label, variable=var,
                           command=lambda k=key: self._toggle_switch(k),
                           bg=COLORS["bg"], fg=COLORS["text"],
                           activebackground=COLORS["bg"],
                           activeforeground=COLORS["text"],
                           selectcolor=COLORS["accent"],
                           font=FONT_UI, anchor="w",
                           highlightthickness=0, bd=0).pack(anchor="w")

        # ---- 云端 API（2026-10-27）----
        # 用户要求「在设置里就能打开，并且能选哪个模型走 API」。
        # 角色选哪个走 API 在上面每行的「来源」下拉；这里管**总开关与白名单**。
        tk.Frame(page, bg=COLORS.get("panel_hi", "#1F2544"),
                 height=1).pack(fill="x", pady=(10, 6))
        tk.Label(page, text="云端 API", bg=COLORS["bg"], fg=COLORS["text"],
                 font=FONT_TITLE).pack(anchor="w")
        tk.Label(page, text="⚠ 打开后，走 API 的角色会把对话内容发到外面。"
                            "画像/记忆/价值观/人格提示在网关里写死为永不外发。",
                 bg=COLORS["bg"], fg=COLORS["warn"], font=FONT_SMALL,
                 justify="left", wraplength=560).pack(anchor="w", pady=(2, 6))

        self._api_sw: dict[str, tk.BooleanVar] = {}
        for key, label, hint in (
                ("enabled", "允许使用云端能力（总开关）",
                 "关着的时候，任何走 API 的角色都会被拒绝"),
                ("allow_api_for_chat", "允许用云端生成对话",
                 "走 API 的角色处理的就是对话内容，这一项必须开"),
                ("redact", "外发前脱敏（手机号/邮箱/证件号/长数字）", ""),
        ):
            var = tk.BooleanVar(value=False)
            self._api_sw[key] = var
            row = tk.Frame(page, bg=COLORS["bg"])
            row.pack(anchor="w", fill="x")
            tk.Checkbutton(row, text=label, variable=var,
                           command=lambda k=key: self._save_api_switches(),
                           bg=COLORS["bg"], fg=COLORS["text"],
                           activebackground=COLORS["bg"],
                           activeforeground=COLORS["text"],
                           selectcolor=COLORS["accent"], font=FONT_UI,
                           anchor="w", highlightthickness=0, bd=0).pack(side="left")
            if hint:
                tk.Label(row, text=f"（{hint}）", bg=COLORS["bg"],
                         fg=COLORS["text_dim"], font=FONT_SMALL).pack(side="left")

        # 白名单 + 每日上限
        line = tk.Frame(page, bg=COLORS["bg"])
        line.pack(anchor="w", fill="x", pady=(4, 0))
        tk.Label(line, text="允许的 API 名单", bg=COLORS["bg"],
                 fg=COLORS["text"], font=FONT_UI).pack(side="left")
        self._api_list_var = tk.StringVar(value="")
        e1 = tk.Entry(line, textvariable=self._api_list_var, width=34, font=FONT_UI)
        e1.pack(side="left", padx=(6, 4))
        e1.bind("<FocusOut>", lambda _e: self._save_api_switches())
        tk.Label(line, text="（逗号分隔，对应角色的 api_name）", bg=COLORS["bg"],
                 fg=COLORS["text_dim"], font=FONT_SMALL).pack(side="left")

        line2 = tk.Frame(page, bg=COLORS["bg"])
        line2.pack(anchor="w", fill="x", pady=(2, 0))
        tk.Label(line2, text="每日调用上限", bg=COLORS["bg"],
                 fg=COLORS["text"], font=FONT_UI).pack(side="left")
        self._api_budget_var = tk.StringVar(value="0")
        e2 = tk.Entry(line2, textvariable=self._api_budget_var, width=8, font=FONT_UI)
        e2.pack(side="left", padx=(6, 4))
        e2.bind("<FocusOut>", lambda _e: self._save_api_switches())
        tk.Label(line2, text="（0 = 不限；超了会拒发）", bg=COLORS["bg"],
                 fg=COLORS["text_dim"], font=FONT_SMALL).pack(side="left")

        self._api_status = tk.Label(page, text="", bg=COLORS["bg"],
                                    fg=COLORS["text_dim"], font=FONT_SMALL,
                                    justify="left", wraplength=560)
        self._api_status.pack(anchor="w", pady=(6, 0))
        self._load_api_switches()

        self._role_status = tk.Label(page, text="", bg=COLORS["bg"],
                                     fg=COLORS["text_dim"], font=FONT_SMALL,
                                     justify="left", wraplength=560)
        self._role_status.pack(anchor="w", pady=(6, 0))
        tk.Label(page, text="常驻建议：对话 + 嵌入 + 记忆（剩约 5.7 GB）。"
                            "思考/判官按需；**思考模型常驻要三思**——三个常驻 + 9B "
                            "只剩 0.44 GB（实测），会拖慢整机。"
                            "同一个模型配给多个角色时**只会起一份**，不用手动回避。",
                 bg=COLORS["bg"], fg=COLORS["text_dim"], font=FONT_SMALL,
                 justify="left", wraplength=560).pack(anchor="w", pady=(4, 0))



    # ---------------- 云端 API 开关（2026-10-27） ----------------
    def _load_api_switches(self) -> None:
        """把 `config/api.yaml` 的当前值读进控件。

        ⚠ 读失败（文件坏了）不能装作"已关闭"蒙混过去——
          要把原因显示在状态行上，否则用户会以为开关没生效。
        """
        try:
            from cloud import api_config
            snap = api_config.snapshot()
        except Exception as exc:
            snap = {"error": f"{type(exc).__name__}: {exc}"}
        if snap.get("error"):
            self._api_status.configure(
                text=f"读取 config/api.yaml 失败：{snap['error']}",
                fg=COLORS.get("err", "#FF7A7A"))
            return
        self._api_sw["enabled"].set(bool(snap.get("enabled")))
        self._api_sw["allow_api_for_chat"].set(bool(snap.get("allow_chat")))
        self._api_sw["redact"].set(bool(snap.get("redact")))
        self._api_list_var.set("，".join(snap.get("allowed_apis") or []))
        self._api_budget_var.set(str(snap.get("max_calls") or 0))
        self._refresh_api_status(snap)

    def _refresh_api_status(self, snap: dict | None = None) -> None:
        """状态行：**哪些角色走 API、现在放不放行**。"""
        try:
            if snap is None:
                from cloud import api_config
                snap = api_config.snapshot()
        except Exception as exc:
            self._api_status.configure(text=f"状态读取失败：{exc}")
            return
        roles = snap.get("api_roles") or []
        if not roles:
            self._api_status.configure(
                text="当前没有角色走 API（在上面把「来源」改成远程 API 即可）",
                fg=COLORS["text_dim"])
            return
        labels = {r: models_config.ROLE_LABELS.get(r, r) for r in models_config.ROLES}
        blocked = snap.get("blocked") or {}
        parts = []
        for r in roles:
            reason = blocked.get(r) or ""
            if reason:
                parts.append(f"✗ {labels.get(r, r)}：{reason}")
            else:
                parts.append(f"✓ {labels.get(r, r)}：可以外发")
        self._api_status.configure(
            text="\n".join(parts),
            fg=(COLORS.get("warn", "#FFCC66") if any(blocked.get(r) for r in roles)
                else COLORS.get("ok", "#57D1A0")))

    def _save_api_switches(self) -> None:
        """把界面上的值写回 `config/api.yaml`（**行级替换，保住注释**）。"""
        try:
            from cloud import api_config
            ok_all = True
            for key in ("enabled", "allow_api_for_chat", "redact"):
                var = self._api_sw.get(key)
                if var is None:
                    continue
                ok_all = api_config.set_bool(key, bool(var.get())) and ok_all
            raw = (self._api_list_var.get() or "").replace("，", ",")
            items = [x.strip() for x in raw.split(",") if x.strip()]
            ok_all = api_config.set_list("allowed_apis", items) and ok_all
            try:
                budget = int((self._api_budget_var.get() or "0").strip() or 0)
            except ValueError:
                budget = 0
            ok_all = api_config.set_int("max_calls_per_day", budget,
                                        section="budget") and ok_all
            self._load_api_switches()
            if not ok_all:
                self._api_status.configure(
                    text="⚠ 部分项没写进 config/api.yaml（键不存在？）",
                    fg=COLORS.get("warn", "#FFCC66"))
            # 开关会影响 is_served（网关拒绝时角色算不可用），刷新角色状态
            try:
                models_config.forget_alive()
            except Exception:
                pass
            self._refresh_model_roles()
        except Exception as exc:
            self._api_status.configure(
                text=f"保存失败：{type(exc).__name__}: {exc}",
                fg=COLORS.get("err", "#FF7A7A"))

    def _open_replies(self) -> None:
        """用系统默认程序打开 `config/replies.yaml`（不存在就先按默认值生成）。

        ⚠ 用 `os.startfile` 而不是内置编辑器：
          · 用户很可能想用 VSCode/记事本按自己习惯改，内置对话框反而难用；
          · 改完**存盘即生效**（`engine/replies.py` 按 mtime 热加载），不用重启。
        ⚠ 打开失败（没关联程序等）要**把路径显示出来**，不能让按钮像没反应。
        """
        try:
            from engine import replies as replies_mod
            path = replies_mod.ensure_file()
        except Exception as exc:
            self._role_status.configure(
                text=f"生成固定回复文件失败：{type(exc).__name__}: {exc}")
            return
        opened = False
        try:
            import os
            os.startfile(str(path))          # Windows 专用：交给默认程序
            opened = True
        except Exception:
            try:
                import subprocess
                subprocess.Popen(["notepad.exe", str(path)])
                opened = True
            except Exception:
                opened = False
        if opened:
            self._role_status.configure(
                text=f"已打开固定回复文件：改完保存即生效（不用重启）\n{path}")
        else:
            self._role_status.configure(
                text=f"打不开编辑器，请手动编辑这个文件（改完保存即生效）：\n{path}")

    def _toggle_switch(self, key: str) -> None:
        """勾选/取消一个开关 → **立刻写回** `config/models.yaml`。

        ⚠⚠ 2026-10-27：这个方法一度**整个消失了**——
          我之前替换相邻方法（`_toggle_source`）时，按"从头到 `_save_roles`"整段替换，
          把夹在中间的 `_toggle_switch` 一起删掉了。
          症状：点任何勾选框 → `AttributeError: no attribute '_toggle_switch'`，
          终端一片红，**设置一项都没保存**，而界面上勾选框还会自己变回去。
          → 已补回；并新增 `tests/ui_wiring_test.py` 专门盯"界面引用的方法必须存在"。

        ⚠ 顺带一条：`selectcolor` 必须用**亮色**。
          原来设成深色面板色，勾上和不勾在深色底上**长得一模一样**，
          用户根本分不清有没有生效（这也会被误当成"保存失败"）。
        """
        var = (getattr(self, "_switch_vars", {}) or {}).get(key)
        if var is None:
            return
        try:
            models_config.set_switch(key, bool(var.get()))
            text = f"已保存：{key} = {bool(var.get())}"
            # 自动拉起关掉时，说清楚"这次不会再自动起模型"
            if key == "autostart" and not var.get():
                text += "（下次开界面不会自动拉起模型）"
            self._role_status.configure(text=text)
        except Exception as exc:
            self._role_status.configure(text=f"保存失败：{type(exc).__name__}: {exc}")
        # 开关会影响角色可用性（比如 auto_think），顺手刷一下状态
        try:
            self._refresh_model_roles()
        except Exception:
            pass

    def _toggle_source(self, role: str) -> None:
        """切「本地模型 / 远程 API」时换字段组。

        · 本地：模型下拉 + 端口 / ngl / ctx
        · API ：api_base / api_model / api_key_env（跨 4 列，顶掉上面那四格）

        ⚠ 全部用 `grid_remove()` / `grid()`：grid 会**记住原来的行列表位**，
          切回来不用重新指定 `row/column`。
        ⚠ 千万不要用 `pack(before=另一个还没 pack 的控件)`——Tk 会直接抛
          `window "…" isn't packed`（踩过）。
        """
        try:
            label = (self._source_vars.get(role).get()
                     if self._source_vars.get(role) else "本地模型")
            is_api = (label == models_config.SOURCE_LABELS["api"])

            api_holder = self._api_holders.get(role)
            model_box = self._model_boxes.get(role)
            params = self._param_widgets.get(role) or {}

            if is_api:
                # 隐藏本地那一组
                if model_box is not None:
                    model_box.grid_remove()
                for w in params.values():
                    w.grid_remove()
                if api_holder is not None:
                    api_holder.grid()
            else:
                if api_holder is not None:
                    api_holder.grid_remove()
                if model_box is not None:
                    model_box.grid()
                for w in params.values():
                    w.grid()
            self._refresh_model_roles()
        except Exception as exc:
            print(f"（切换来源时出错，忽略：{exc}）")

    def _save_roles(self) -> None:
        # ⚠ 只提交**真的变了**的键；参数留空就跳过（不然空串会把配置写坏）
        updates = {}
        for role, var in self._role_vars.items():
            item = {"model": var.get()}
            for key, pvar in (self._param_vars.get(role) or {}).items():
                raw = (pvar.get() or "").strip()
                if raw:
                    try:
                        item[key] = int(raw)
                    except ValueError:
                        self._role_status.configure(
                            text=f"{models_config.ROLE_LABELS[role]} 的 {key} 不是整数，已跳过")
            rvar = self._resident_vars.get(role)
            if rvar is not None:
                item["resident"] = bool(rvar.get())
            # ---- 来源 + API 字段 ----
            src_var = self._source_vars.get(role)
            if src_var is not None:
                label = str(src_var.get())
                item["source"] = next(
                    (s for s, lab in models_config.SOURCE_LABELS.items()
                     if lab == label), "local")
            for key, av in (self._api_vars.get(role) or {}).items():
                raw = (av.get() or "").strip()
                if raw:
                    item[key] = raw
            updates[role] = item
        try:
            models_config.save(updates)
            self._role_status.configure(text="已保存到 config/models.yaml")
        except Exception as exc:
            self._role_status.configure(text=f"保存失败：{exc}")
        self._refresh_model_roles()

    def _refresh_model_roles(self) -> None:
        """刷新角色状态（**网络探测放后台线程**，绝不占 UI 线程）。

        ## 为什么必须异步

        实测：本机连**关闭的**本地端口不会立刻被拒，而是跑满超时
        （`socket.connect` 到空端口要 1500 ms）。于是 5 个角色串行探活 = **7.7 秒**，
        全部卡在 UI 线程上 → 一打开设置窗口就"卡一下"。

        现在三道一起改：
          · `_alive` 超时 1.5 → **0.35 秒**
          · `running_roles()` **并发**探活
          · 这里**再放后台线程**（就算 0.4 秒也不许卡界面）

        线程里只算数据，**碰控件全部回到主线程**（Tk 不是线程安全的）。
        """
        import threading

        def work() -> None:
            try:
                running = models_config.running_roles()
                shares = {r: models_config.shared_with(r) for r in models_config.ROLES}
                ports = {r: models_config.effective_port(r) for r in models_config.ROLES}
                cfg = models_config.load()["roles"]
                judge_ep = models_config.judge_endpoint()
            except Exception as exc:
                running, shares, ports = {}, {}, {}
                cfg, judge_ep = {}, f"（读取失败 {exc}）"
            payload = (running, shares, ports, cfg, judge_ep)
            try:
                # ⚠ `SettingsWindow` 是 `tk.Toplevel`，**没有 `root` 属性**。
                #   原来写 `self.root.after(...)` → AttributeError 被 except 吞掉，
                #   结果**状态列永远是「⋯ 探测中」**（真实症状：状态一直不刷新）。
                #   Toplevel 自己就有 `after`，用 `self.after` 即可。
                self.after(0, lambda: self._apply_model_roles(payload))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _apply_model_roles(self, payload) -> None:
        """把后台探到的结果贴到界面上（**只在主线程跑**）。"""
        try:
            running, shares, ports, cfg, judge_ep = payload
        except Exception:
            return
        ok_color = COLORS.get("ok") or "#39c07a"
        for role, lbl in (getattr(self, "_role_hint", {}) or {}).items():
            shared = (shares or {}).get(role)
            eff = (ports or {}).get(role)
            ok = (running or {}).get(role)
            if shared:
                # **防呆的可见部分**：告诉用户"没另起，用的是别人的服务"
                text = f"● 共用 {models_config.ROLE_LABELS.get(shared, shared)} {eff}"
            else:
                text = ("● 在跑" if ok else "○ 没跑") + f" {eff}"
            try:
                lbl.configure(text=text, fg=ok_color if ok else COLORS["text_dim"])
            except Exception:
                pass
        note = f"判官当前：{judge_ep}" if judge_ep else "判官当前：无可用（跳过接地判定）"
        try:
            self.service_box.configure(state="normal")
            self.service_box.delete("1.0", "end")
            self.service_box.insert("1.0", note + "\n\n")
            for role in models_config.ROLES:
                item = (cfg or {}).get(role) or {}
                self.service_box.insert(
                    "end", f"{models_config.ROLE_LABELS[role]}：{item.get('model')}"
                           f"（端口 {item.get('port')}，ngl {item.get('ngl')}"
                           f"{'，常驻' if item.get('resident') else '，按需'}）\n")
            self.service_box.configure(state="disabled")
        except Exception:
            pass

    def _start_think(self) -> None:
        """拉起思考模型（难题才用）。按开关**先关判官模型**腾内存。

        ⚠ 实测：9B 5.2 GB + 4B 2.6 GB 在三个常驻之上放不下
          （五个全上只剩 0.03 GB）。它们从不同时需要，所以互相顶掉。
        """
        from services import autostart
        try:
            r = autostart.start_think(verbose=False)
        except Exception as exc:
            self._role_status.configure(text=f"拉起失败：{exc}")
            return
        msg = f"思考模型：{r.get('action')}　{r.get('detail') or ''}"
        if r.get("stopped"):
            msg += f"　（先停了端口 {r['stopped']}）"
        self._role_status.configure(text=msg[:400])
        try:
            models_config.forget_alive()   # 起停后清探活缓存，别让界面滞后
        except Exception:
            pass
        self._refresh_model_roles()

    def _start_all_models(self) -> None:
        """**一键拉起**：把**勾了常驻**的角色都起来（含思考模型，如果勾了）。

        ⚠ 起之前先做**防呆**：如果目标角色和某个**已经在跑**的角色是同一个模型文件，
          `shared_with()` 会认出来 → 直接复用，不再起一份。
        ⚠ 全起（五个都勾）会把内存吃到 0.03 GB（实测），界面上有提示；这里不拦，
          因为用户可能就是想这么试，但会在状态里如实报告。
        """
        from services import autostart as _as
        cfg = models_config.load()["roles"]
        lines = []
        started_any = False
        for role in models_config.ROLES:
            item = cfg.get(role) or {}
            if not item.get("resident"):
                continue
            # **防呆**：和别人共用就不另起
            try:
                shared = models_config.shared_with(role)
            except Exception:
                shared = None
            if shared:
                lines.append(f"{models_config.ROLE_LABELS[role]}：共用 "
                             f"{models_config.ROLE_LABELS.get(shared, shared)}")
                continue
            fn = {
                "chat": _as.ensure_chat_service,
                "embed": _as.ensure_embed_service,
                "memory": _as.ensure_extract_service,
                "think": _as.start_think,
                "judge": None,
            }.get(role)
            if fn is None:
                continue          # judge 没有独立拉起函数，按需由判官选择处理
            try:
                r = fn(port=int(item.get("port") or 0)) if role != "think" else fn(verbose=False)
                lines.append(f"{models_config.ROLE_LABELS[role]}：{r.get('action')}")
                started_any = started_any or r.get("action") == "started"
            except Exception as exc:
                lines.append(f"{models_config.ROLE_LABELS[role]}：失败 {exc}")
        if not lines:
            lines = ["没有勾选任何常驻角色（在「常驻」那一列打勾再试）"]
        self._role_status.configure(text="　".join(lines)[:420])
        try:
            models_config.forget_alive()   # 起停后清探活缓存，别让界面滞后
        except Exception:
            pass
        self._refresh_model_roles()

    def _stop_all_models(self) -> None:
        from services.manager import ServiceManager
        cfg = models_config.load()["roles"]
        stopped = []
        mgr = ServiceManager()
        for role in models_config.ROLES:
            port = int((cfg.get(role) or {}).get("port") or 0)
            try:
                mgr.stop(port)
                stopped.append(f"{models_config.ROLE_LABELS[role]}({port})")
            except Exception:
                pass
        self._role_status.configure(
            text=("已停：" + "、".join(stopped)) if stopped else "没有在跑的服务")
        try:
            models_config.forget_alive()   # 起停后清探活缓存，别让界面滞后
        except Exception:
            pass
        self._refresh_model_roles()

    # ---------------- 行为 ----------------
    def _changed(self, key: str, value) -> None:
        self.ui_state[key] = value
        if callable(self.on_change):
            try:
                self.on_change(key, value)
            except Exception:
                pass

    def _refresh_services(self) -> None:
        text = []
        try:
            if self.manager is not None:
                for item in self.manager.status([8080, 8081]):
                    state = "健康" if item.get("healthy") else "未响应"
                    name = str(item.get("model") or "").replace("\\", "/").split("/")[-1]
                    text.append(f"[{item['port']}] {state}　{name or '—'}")
        except Exception as exc:
            text.append(f"（读取失败：{exc}）")
        if not text:
            text.append("（没有在跑的服务；用 启动模型.bat 或 启动llm-Heretic.bat 启动）")
        self.service_box.delete("1.0", "end")
        self.service_box.insert("1.0", "\n".join(text))

    def _refresh_memory(self) -> None:
        text = []
        try:
            if self.companion is not None:
                text.append(f"语义记忆：{self.companion.memory.semantic_count()} 条")
                text.append(f"情景记忆：{self.companion.episode_count()} 条")
                text.append(f"当前会话：{getattr(self.companion, 'session', '—')}")
        except Exception as exc:
            text.append(f"（读取失败：{exc}）")
        if not text:
            text.append("（未连接记忆库）")
        self.memory_box.delete("1.0", "end")
        self.memory_box.insert("1.0", "\n".join(text))

    def _wipe(self) -> None:
        if callable(self.on_wipe):
            try:
                self.on_wipe()
            except Exception as exc:
                print(f"清除失败：{exc}")
        self._refresh_memory()


def open_settings(master, **kwargs) -> SettingsWindow:
    """打开设置窗口（已经开着就置顶，不重复开）。"""
    existing = getattr(master, "_settings_window", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        existing.focus_set()
        return existing
    win = SettingsWindow(master, **kwargs)
    try:
        master._settings_window = win
    except Exception:
        pass
    return win


if __name__ == "__main__":                       # 单独预览这个窗口
    root = tk.Tk()
    root.withdraw()
    open_settings(root)
    root.mainloop()
