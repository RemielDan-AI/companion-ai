"""界面视觉底座（精装修）：配色/字体/圆角卡片/气泡/打字指示/角色区/状态区。

为什么单独成一个模块：不改动 gui.py 的现有逻辑，先把"长什么样"做出来，
你可以先跑预览看效果，满意再逐个接进主界面。

预览：python gui_theme.py            （弹出预览窗口）
自检：python gui_theme.py --selftest （构建一遍后自动退出，用于验证不报错）
"""
from __future__ import annotations

import argparse
import sys
import tkinter as tk
from tkinter import font as tkfont

# ---------------- 配色（DeepSeek 蓝主调 + 鲸鱼意象；原创配色，不涉及任何素材） ----------------
COLORS = {
    "bg": "#0F1220",            # 深海夜色
    "panel": "#161A2E",         # 面板
    "panel_hi": "#1F2544",      # 面板高亮/悬停
    "bubble_me": "#2A3566",     # 用户气泡
    "bubble_ai": "#1A1F38",     # AI 气泡
    "bubble_border": "#2A3155",
    # 对话区**选中高亮**。⚠ 这两个色必须跟上面三个气泡底色都明显不同：
    #   每一行都挂了带 background 的 tag，而 Tk 里**后建的 tag 优先级更高**，
    #   内建的 `sel` 是**最早建**的 → 选区会被气泡底色盖住，**选中了也看不见**
    #   （用户反馈"没法用鼠标选中复制"的真因）。
    #   配合 gui.py 里的 `tag_raise("sel")` 一起用，见那边的注释。
    "select_bg": "#4D6BFE",     # 选中背景（亮蓝，压过 #2A3566 / #1A1F38 / #0F1220）
    "select_fg": "#FFFFFF",     # 选中前景
    "accent": "#4D6BFE",        # 鲸蓝主色
    "accent_soft": "#6B8AFF",   # 浅一档的蓝
    "accent_deep": "#2B3A8F",   # 深一档的蓝（兜帽/尾鳍）
    "accent_warm": "#FFB86B",   # 暖色点缀（情绪/光晕）
    "skin": "#FFE3D3",          # 角色肤色（自绘占位用）
    "blush": "#FF9BAE",
    "text": "#E8ECFF",
    "text_dim": "#9AA6CC",
    "ok": "#57D1A0",
    "warn": "#FFCC66",
    "err": "#FF7A7A",
}

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_UI_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 12, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_BODY = ("Microsoft YaHei UI", 11)


def _reply_word(key: str, default: str) -> str:
    """取一个界面固定词（`config/replies.yaml`）。"""
    try:
        from engine import replies
        return replies.get(key, default)
    except Exception:
        return default


def _placeholder() -> str:
    return _reply_word("placeholder_reply",
                        "那就慢慢说，我在这儿听着。双击这行可以重听。")


def pick_font(root: tk.Misc, base: int | None = None,
              family: str | None = None) -> None:
    """按住系统里实际存在的字体兜底（换机器不至于变方框）。

    ## ⚠ 2026-10-27：以前字号是**写死**的（10/12/9/11）

    于是「界面字号」「设置窗口字号」那些下拉**改了也没用**——用户看到的
    选择项形同虚设。现在字号由 `base` 推出来：

        base      → FONT_SMALL      base + 1 → FONT_UI / FONT_UI_BOLD
        base + 2  → FONT_BODY       base + 3 → FONT_TITLE

    ⚠ **"按值导入"的坑**：`from gui_theme import FONT_UI` 拿到的是**当时的元组**，
      改 `gui_theme.FONT_UI` 不会更新别人手里的副本。
      调用方改完要用 `sync_fonts(globals())` 重新取一次（在建控件之前）。
    """
    families = set(tkfont.families(root))
    global FONT_UI, FONT_UI_BOLD, FONT_TITLE, FONT_SMALL, FONT_BODY
    try:
        b = int(base) if base is not None else 9
    except Exception:
        b = 9
    b = max(7, min(22, b))
    candidates = tuple((family,) if family else ()) + (
        "Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "SimHei")
    for name in candidates:
        if name and name in families:
            FONT_UI = (name, b + 1)
            FONT_UI_BOLD = (name, b + 1, "bold")
            FONT_TITLE = (name, b + 3, "bold")
            FONT_SMALL = (name, b)
            FONT_BODY = (name, b + 2)
            return


def sync_fonts(namespace: dict) -> None:
    """把 gui_theme 当前的字体元组**重新灌进调用方的模块命名空间**。

    为"按值导入"那个坑准备的：`namespace` 传 `globals()`，在**建控件之前**调。
    """
    for key in ("FONT_UI", "FONT_UI_BOLD", "FONT_TITLE", "FONT_SMALL", "FONT_BODY"):
        namespace[key] = globals()[key]


def restyle_tree(widget, mapping: dict) -> None:
    """按「旧字体元组 → 新字体元组」改掉整棵控件树的字体。

    给"改完立即生效"用：主窗口的 `tk.Label`/`tk.Button` 建一次就固定了，
    `ttk` 的能靠 `style.configure`，`tk` 的不重新走一遍就永远是老字号。
    """
    try:
        current = widget.cget("font")
        if current:
            key = tuple(current) if isinstance(current, (list, tuple)) else str(current)
            hit = mapping.get(key) or mapping.get(str(key))
            if hit:
                widget.configure(font=hit)
    except Exception:
        pass
    for child in widget.winfo_children():
        restyle_tree(child, mapping)


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius=12, **kwargs):
    """在 Canvas 上画圆角矩形（Tkinter 没有原生圆角）。"""
    points = [x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
              x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
              x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class Bubble(tk.Frame):
    """一条消息气泡：头像 + 圆角气泡 + 悬停高亮（像 IM）。"""

    def __init__(self, master, text: str, mine: bool = False, name: str = "未央",
                 on_replay=None, **kw):
        super().__init__(master, bg=COLORS["bg"], **kw)
        self.mine = mine
        self._hover = False

        avatar = tk.Canvas(self, width=34, height=34, bg=COLORS["bg"],
                           highlightthickness=0)
        avatar.create_oval(2, 2, 32, 32, fill=COLORS["bubble_me"] if mine
                           else COLORS["accent"], outline="")
        avatar.create_text(17, 17, text="我" if mine else "央",
                           fill=COLORS["text"], font=FONT_SMALL)
        avatar.pack(side="right" if mine else "left", padx=(8, 8), pady=(4, 4))

        holder = tk.Frame(self, bg=COLORS["bg"])
        holder.pack(side="right" if mine else "left", fill="x", expand=True)
        self.canvas = tk.Canvas(holder, bg=COLORS["bg"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.label = tk.Label(holder, text=text, bg=COLORS["bubble_ai"],
                              fg=COLORS["text"], font=FONT_BODY, justify="left",
                              wraplength=460, padx=12, pady=8, anchor="w")
        self._theme(False)
        self._on_replay = on_replay
        self._bind_hover(holder, self.label, self.canvas)

    def _theme(self, hover: bool) -> None:
        self.label.configure(bg=COLORS["bubble_me"] if self.mine
                             else (COLORS["panel_hi"] if hover else COLORS["bubble_ai"]))

    def _bind_hover(self, *widgets) -> None:
        def enter(_event=None):
            self._hover = True
            self._theme(True)

        def leave(_event=None):
            self._hover = False
            self._theme(False)

        for widget in widgets:
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)
            if self._on_replay:
                widget.bind("<Double-Button-1>", lambda _e: self._on_replay())


class TypingIndicator(tk.Frame):
    """打字指示：三个点循环跳动（生成中显出来，出结果再销毁）。"""

    def __init__(self, master, **kw):
        super().__init__(master, bg=COLORS["bg"], **kw)
        tk.Label(self, text="未央正在想", bg=COLORS["bg"], fg=COLORS["text_dim"],
                 font=FONT_SMALL).pack(side="left")
        self.dots = []
        for _ in range(3):
            dot = tk.Label(self, text="·", bg=COLORS["bg"], fg=COLORS["accent"],
                           font=("Microsoft YaHei UI", 14, "bold"))
            dot.pack(side="left")
            self.dots.append(dot)
        self._step = 0
        self._alive = True
        self._tick()

    def _tick(self) -> None:
        if not self._alive:
            return
        for index, dot in enumerate(self.dots):
            dot.configure(fg=COLORS["accent"] if index == self._step % 3
                          else COLORS["panel_hi"])
        self._step += 1
        self.after(320, self._tick)

    def stop(self) -> None:
        self._alive = False
        self.destroy()


def _find_character_asset() -> str:
    """找一张可用的角色形象：优先 config/gui.yaml 指定，其次**本项目里的图**，
    最后退回皮肤包里最大的竖版图。

    ## 查找顺序（2026-10-24 改）

        ① `config/gui.yaml: character_asset`
             · **绝对路径**直接用
             · 相对路径依次试：本项目根目录 / 本项目 assets 目录 / 皮肤目录
        ② **本项目根目录里的图**（放一张竖版图进去就会被找到）
           优先级：`头像/立绘/avatar` 命名的 > 面积最大的竖版
        ③ 皮肤包里最大的竖版图（旧行为，兜底）

    ⚠ 以前只认皮肤目录，用户把图放进项目根目录时**永远看不到**——
      而且 `character_asset: 某张图.png` 这种写法也会因为只在皮肤目录里找而失效。
    """
    import pathlib
    ROOT_DIR = pathlib.Path(__file__).resolve().parent
    # ⚠⚠ 2026-10-27：原来这里**硬编码**了本机的一条皮肤包路径
    #   （本机某条皮肤包路径），
    #   作为「项目里没图」时的兜底。
    #
    #   两个问题：
    #     ① 那是**某台机器的绝对路径**，开源后对别人毫无意义；
    #     ② 它会让程序从 `node_modules` 里**随便抓一张 webp 当立绘**——
    #        实测在净化后的仓库里就抓到了一张无关的界面皮肤图。
    #        （一个陪伴 AI 顶着别人的界面皮肤，很荒谬。）
    #
    #   → 改成配置项 `config/gui.yaml: skin_dir`，**默认空 = 不找皮肤包**。
    #     想用皮肤包就自己填路径。
    skin_cfg = ""
    try:
        import yaml as _yaml
        _cfg = _yaml.safe_load((ROOT_DIR / "config" / "gui.yaml")
                               .read_text(encoding="utf-8")) or {}
        skin_cfg = str(_cfg.get("skin_dir") or "").strip()
    except Exception:
        skin_cfg = ""
    skin = pathlib.Path(skin_cfg) if skin_cfg else None
    IMG_SUFFIX = (".png", ".webp", ".jpg", ".jpeg")

    def _usable(path: pathlib.Path) -> bool:
        return path.is_file() and path.suffix.lower() in IMG_SUFFIX

    override = ""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT_DIR / "config" / "gui.yaml")
                             .read_text(encoding="utf-8")) or {}
        override = str(cfg.get("character_asset") or "")
    except Exception:
        override = ""

    # ① 显式指定
    if override:
        cand = pathlib.Path(override)
        if cand.is_absolute():
            if _usable(cand):
                return str(cand)
        else:
            # ⚠ `skin` 可能是 None（没配 skin_dir）→ 不能直接放进元组里。
            _bases = [ROOT_DIR, ROOT_DIR / "assets", ROOT_DIR / "头像",
                      ROOT_DIR / "立绘"]
            if skin is not None:
                _bases.append(skin)
            for base in _bases:
                if _usable(base / cand):
                    return str(base / cand)

    # ② 本项目里的图
    def _pick_local() -> str:
        best, best_score = "", -1.0
        try:
            from PIL import Image
        except Exception:
            Image = None  # type: ignore[assignment]
        for folder in (ROOT_DIR, ROOT_DIR / "assets", ROOT_DIR / "头像",
                       ROOT_DIR / "立绘"):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if not _usable(path):
                    continue
                name = path.name.lower()
                # ⚠ 跳过 _ 开头的临时/中间产物：调试脚本存图时很容易
                #   往根目录丢 _asset_preview.png 这种，按面积排序会把它选中。
                if name.startswith(_):
                    continue
                # 命名带这些词的图是"这张就是要用的形象"，直接压倒面积排序
                bonus = 3.0 if any(k in name for k in ("头像", "立绘", "avatar",
                                                       "character", "portrait")) else 0.0
                area = 0.0
                ratio_ok = 1.0
                if Image is not None:
                    try:
                        with Image.open(path) as im:
                            w, h = im.size
                        area = w * h / 1e6
                        ratio_ok = 1.0 if h >= w else 0.6      # 横版扣分
                    except Exception:
                        pass
                score = bonus + area * ratio_ok
                if score > best_score:
                    best, best_score = str(path), score
        return best

    local = _pick_local()
    if local:
        return local

    # ③ 皮肤包兜底（旧行为）
    if skin is None or not skin.exists():
        return ""
    best, best_area = "", 0
    try:
        from PIL import Image
        for path in (skin.iterdir() if skin is not None else []):
            if path.suffix.lower() not in (".png", ".webp"):
                continue
            try:
                with Image.open(path) as im:
                    width, height = im.size
            except Exception:
                continue
            if height <= width * 1.2:          # 只要竖版（立绘）
                continue
            if width * height > best_area:
                best, best_area = str(path), width * height
    except Exception:
        return ""
    return best


def _key_out_white(im):
    """把**和四边连通**的白色变成透明，内部的亮部保留。

    ## 为什么必须这么做

    用户给的图常常是白底（实测拿到的立绘就是一张白底的等距水晶环）。
    面板底色是深色 `#161A2E`，白底直接贴上去就是**一块白方块**——
    看起来像图片没加载完，而不是一个发光的形象。

    ## 为什么用泛洪而不是"按亮度抠"

    那张图的**中心是白色发光**。按"白色→透明"整图抠会把中心光也抠掉，留一个洞。
    而背景白是**和四边连通**的、中心白是被环**围住**的——所以从四角泛洪最准：

        · 只清除从边角能连到的白 → 中心的光完好
        · 阈值放宽一点（28）→ 连带把抗锯齿的浅灰边也吃掉，不留白边
    """
    from PIL import ImageDraw
    im = im.convert("RGBA")
    width, height = im.size
    tol = 28
    for seed in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        r, g, b, _a = im.getpixel(seed)
        if r < 255 - tol or g < 255 - tol or b < 255 - tol:
            continue                       # 这个角本来就不是白的，跳过
        ImageDraw.floodfill(im, seed, (0, 0, 0, 0), thresh=tol)
    return im


def _asset_looks_white_backed(path: str) -> bool:
    """这张图是不是**真的白底、需要抠**。抠错比不抠更糟，所以判据要严。

    ## ⚠ 第一版判错了（靠运气）

    原来是 `im.convert("RGB")` 再数四角是不是白。但 `convert("RGB")`
    **把 alpha 丢掉了**：已经带透明通道的图，透明区的 RGB 可能是白也可能是黑——
    实测那张立绘上面两角是 `(255,255,255,0)`、下面两角是 `(0,0,0,0)`，
    转成 RGB 就成了"两白两黑"，数出来 2，**碰巧**小于 3 才没误抠。

    ## 正确判据（三步）

        ① **先看 alpha**：全透明像素 ≥ 30% → 这图**本来就是透明底**，绝不抠
           （它看着"白"只是查看器把透明渲成了白；抠了反而会把内容打洞）
        ② 四角必须**不透明且接近纯白**
        ③ 3 个角以上满足才动手
    """
    try:
        from PIL import Image
        with Image.open(path) as raw:
            im = raw.convert("RGBA")
            width, height = im.size
            px = im.load()
            # ① 已经有可用的透明通道 → 不动
            transparent = sampled = 0
            for y in range(0, height, 4):
                for x in range(0, width, 4):
                    sampled += 1
                    if px[x, y][3] == 0:
                        transparent += 1
            if sampled and transparent / sampled >= 0.30:
                return False
            # ② 四角：不透明 + 接近纯白
            white = 0
            for xy in ((1, 1), (width - 2, 1), (1, height - 2), (width - 2, height - 2)):
                r, g, b, a = px[xy]
                if a > 200 and r > 235 and g > 235 and b > 235:
                    white += 1
            return white >= 3
    except Exception:
        return False



class CharacterPanel(tk.Frame):
    """角色形象区（立绘/Live2D 的占位）：装了立绘就显示立绘，否则自绘鲸鱼娘。

    以后接真立绘/动画时，把 canvas 上的内容换成图片或逐帧绘制即可，
    外层接口（set_state / set_mood）不用改。
    """

    def __init__(self, master, name: str = "未央", width: int = 220,
                 height: int = 300, **kw):
        super().__init__(master, bg=COLORS["panel"], **kw)
        self._art_w, self._art_h = width, height
        # 底部留一条**文字带**给名字/状态：不留的话字会压在立绘上，
        # 而 Tk Canvas 没有"文字背景"，压上去就糊了。
        self._text_h = 46
        self.canvas = tk.Canvas(self, width=width, height=height, bg=COLORS["panel"],
                                highlightthickness=0)
        self.canvas.pack(padx=10, pady=(12, 4))
        self._glow = None
        self._phase = 0
        self._mood = "calm"
        self._alive = True
        self._art = None
        self._load_art()
        # ⚠ 2026-10-27：名字/状态原来是的两个 `tk.Label`，**放在画布下面**。
        #   左栏高度一紧（输入区变高之后），画布把高度占满，
        #   `state_label` 直接被挤出可视区（实测 mapped=0），
        #   界面上就只剩一张图、没有名字。
        #   → 改成**画进 canvas 里**：文字跟着画布走，永远不会被裁。
        self._name_text = str(name or "未央")
        self._state_text = _reply_word("listening", "在听你说")
        # 保留这两个名字（外部只读它们做存在性检查，不再当控件用）
        self.name_label = None
        self.state_label = None
        self._draw()
        self._breathe()

    def _load_art(self) -> None:
        """把形象按面板尺寸缩放并居中取景（保持比例，不拉伸变形）。

        ⚠ 2026-10-24：**白底的图先抠成透明底**再缩放。
          不抠的话在深色面板上就是一块白方块，看着像图没加载完。
          只在"四角确实是白的"时才抠（`_asset_looks_white_backed`）——
          **抠错比不抠更糟**：把有内容的浅色图抠成洞就没救了。
        """
        path = _find_character_asset()
        if not path:
            return
        try:
            from PIL import Image, ImageTk
            with Image.open(path) as raw:
                im = raw.convert("RGBA")
            if _asset_looks_white_backed(path):
                try:
                    im = _key_out_white(im)
                except Exception as exc:
                    print(f"（白底抠除失败，保留原图：{exc}）")
            # 抠完之后按**不透明像素**的包围盒裁掉多余空白，
            # 让形象在面板里占满，而不是缩成中间一小块。
            try:
                bbox = im.getbbox()
                if bbox:
                    im = im.crop(bbox)
            except Exception:
                pass
            pad = 0.06                                  # 两侧留一点呼吸
            target_w = max(1, int(self._art_w * (1 - pad)))
            # ⚠ 高度要减掉底部文字带，否则立绘会伸到名字底下、字压图
            target_h = max(1, int((self._art_h - self._text_h) * (1 - pad)))
            scale = min(target_w / im.width, target_h / im.height)
            size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
            im = im.resize(size, Image.LANCZOS)
            self._art = ImageTk.PhotoImage(im)
            # 记下实际尺寸，`_draw` 要按它居中（原图比例和面板不一致）
            self._art_size = im.size
        except Exception as exc:
            print(f"（立绘加载失败，改用自绘形象：{exc}）")
            self._art = None
            self._art_size = (0, 0)

    @staticmethod
    def _mix(base_hex: str, top_hex: str, ratio: float) -> str:
        """把 `top` 按比例混进 `base`，模拟"淡"（Tk Canvas 没有透明度）。

        ratio=0 → 完全是底色（看不见），ratio=1 → 完全是 top（很亮）。
        画"若有若无的光晕"就用 0.1 左右。
        """
        def _rgb(hx: str) -> tuple[int, int, int]:
            hx = str(hx or "#000000").lstrip("#")
            if len(hx) == 3:
                hx = "".join(ch * 2 for ch in hx)
            try:
                return (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16))
            except Exception:
                return (0, 0, 0)
        r = max(0.0, min(1.0, float(ratio)))
        br, bg_, bb = _rgb(base_hex)
        tr, tg, tb = _rgb(top_hex)
        return "#%02x%02x%02x" % (int(br + (tr - br) * r),
                                  int(bg_ + (tg - bg_) * r),
                                  int(bb + (tb - bb) * r))

    def _draw(self) -> None:
        """画角色区：底层动效 → 立绘 → 上浮粒子。

        ## 2026-10-27 重做底层动画

        原来是"两个同心圆 + 三个上浮小球"，跟这张**等距水晶环**放一起太通用，
        而且那两圈实线会横穿晶体的边。现在改成呼应"环"这个母题：

            ① **三层光晕**：极暗的同心圆，半径缓慢呼吸（`_phase` 驱动）
            ② **旋转弧段**：三段约 55° 的弧，绕中心慢转（像环在缓慢自转）
            ③ **环绕光点**：5 个不同半径/相位的小点绕行
            ④ 立绘本身
            ⑤ **上浮粒子**：保留但调暗调小，别抢主体

        ⚠ Tk 的 Canvas **没有透明度**，所以"淡"是靠**把颜色往面板底色混**实现的
          （见 `_mix`）。直接用 accent 会太亮、压住立绘。
        ⚠ 全部用 `create_oval/arc` 的 `outline` 画**线**，不填充——
          实心块会把立绘挡掉，而 Tk 没有 z-index 之外的透明叠加。
        """
        c = self.canvas
        c.delete("all")
        w, h = self._art_w, self._art_h
        phase = self._phase
        # ⚠ 圆心用**立绘区**的中心（画布高减去底部文字带），
        #   否则光晕/弧段会压到名字上。
        art_cy = (h - self._text_h) // 2
        cx, cy = w // 2, art_cy
        warm = self._mood == "warm"
        accent = COLORS["accent_warm"] if warm else COLORS["accent"]
        soft = COLORS.get("accent_soft", "#6B8AFF")
        base = COLORS.get("bg", "#0F1220")

        # ① 三层光晕：半径缓慢呼吸 ±3px（phase 0..23 → 一个完整周期）
        breath = (phase if phase <= 12 else 24 - phase)      # 0..12..0 的三角波
        for i, frac in enumerate((0.30, 0.36, 0.43)):
            radius = int((h - self._text_h) * frac) + breath // 3
            # 越外圈越暗
            ring_color = self._mix(base, accent, 0.10 - i * 0.02)
            c.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                          outline=ring_color, width=1)

        # ② 三段旋转弧（环在自转）：每段 55°，间隔 120°，整体按 phase 转
        arc_r = int((h - self._text_h) * 0.44)
        for i in range(3):
            start = (phase * 4 + i * 120) % 360
            c.create_arc(cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r,
                         start=start, extent=55, style="arc",
                         outline=self._mix(base, soft, 0.55), width=2)

        # ③ 五个环绕光点：半径/相位都错开，看着不机械
        import math as _math
        for i, (frac, deg_off, size) in enumerate((
                (0.34, 0, 3), (0.40, 72, 2), (0.46, 144, 2),
                (0.37, 216, 2), (0.43, 288, 3))):
            ang = _math.radians((phase * 5 + deg_off) % 360)
            px = cx + int(_math.cos(ang) * (h - self._text_h) * frac)
            py = cy + int(_math.sin(ang) * (h - self._text_h) * frac)
            motes = self._mix(base, accent if i % 2 == 0 else soft, 0.75)
            c.create_oval(px - size, py - size, px + size, py + size,
                          outline=motes, width=1)

        # ④ 立绘：**上下浮动**（2026-10-27 新增）
        #   只让图浮、背景光晕与弧段保持不动 —— 这样看得出是"图浮在光里"，
        #   而不是整块区域在上下抖（后者看着像窗口在跳）。
        #   `phase` 24 帧，一个正弦周期取 12 帧 ≈ **3.6 秒**往返一次，
        #   幅度 5px：够看出在动，又不至于让人眼晕。
        bob = int(round(5 * _math.sin(2 * _math.pi * phase / 12)))
        if self._art is not None:
            # ⚠ 按**实际图片尺寸**居中：抠白底+包围盒裁切之后，
            #   图片比例已经和面板不一样了，直接 create_image(cx, cy) 会偏。
            aw, ah = getattr(self, "_art_size", (0, 0)) or (0, 0)
            # 在**立绘区**（画布去掉底部文字带）里居中，再叠加浮动偏移
            c.create_image((w - aw) // 2 + aw // 2 if aw else cx,
                           ((h - self._text_h - ah) // 2 + ah // 2 + bob
                            if ah else cy + bob),
                           image=self._art)
        else:
            self._draw_whale_girl(cx, cy + bob, phase)

        # ⑤ 上浮粒子：调暗调小，只当背景呼吸，别抢立绘
        # ⚠ 循环变量名必须和循环体一致：解包成了 `_base_y`，体内还写 `base`，
        #   而 `base` 在上面已经是**颜色字符串** → `str - int` 直接崩（踩过）。
        for index, (bx, _base_y) in enumerate(((w * 0.14, (h - self._text_h) * 0.88),
                                               (w * 0.88, (h - self._text_h) * 0.66))):
            rise = (phase * 6 + index * 18) % max(1, int((h - self._text_h) * 0.42))
            by = _base_y - rise
            size = 3 + index
            c.create_oval(bx - size, by - size, bx + size, by + size,
                          outline=self._mix(base, COLORS["accent_soft"], 0.45),
                          width=1)

        # ⑥ 名字 + 状态（**画在画布内**，不会被布局挤掉）
        try:
            name_font = tkfont.Font(family=FONT_TITLE[0],
                                    size=int(FONT_TITLE[1]) + 2, weight="bold")
            state_font = tkfont.Font(family=FONT_SMALL[0],
                                     size=int(FONT_SMALL[1]))
            c.create_text(cx, h - 34, text=self._name_text,
                          fill=COLORS.get("text", "#E8ECFF"),
                          font=name_font, anchor="center")
            if self._state_text:
                # 状态用更暗的颜色，层级压在名字下面
                c.create_text(cx, h - 14, text=self._state_text,
                              fill=self._mix(COLORS.get("panel", "#161A2E"),
                                             COLORS.get("text_dim", "#9AA6CC"),
                                             0.85),
                              font=state_font, anchor="center")
        except Exception:
            pass

    def _draw_whale_girl(self, cx: int, cy: int, phase: int) -> None:
        """自绘的"鲸鱼娘"占位（原创矢量式绘制；没有立绘素材时用它兜底）。"""
        c = self.canvas
        top = cy - 62
        # 鲸鱼尾鳍
        c.create_polygon(cx + 34, top + 18, cx + 72, top - 6, cx + 62, top + 30,
                         fill=COLORS["accent_deep"], outline=COLORS["accent_soft"])
        c.create_polygon(cx + 34, top + 18, cx + 72, top + 42, cx + 56, top + 24,
                         fill=COLORS["accent_deep"], outline=COLORS["accent_soft"])
        # 兜帽（鲸头形）+ 内圈
        c.create_oval(cx - 58, top - 4, cx + 58, top + 120,
                      fill=COLORS["accent_deep"], outline="")
        c.create_oval(cx - 46, top + 10, cx + 46, top + 108,
                      fill=COLORS["accent"], outline="")
        # 脸
        c.create_oval(cx - 32, top + 32, cx + 32, top + 96,
                      fill=COLORS["skin"], outline="")
        for eye_x in (cx - 13, cx + 13):
            c.create_oval(eye_x - 6, top + 58, eye_x + 6, top + 72,
                          fill="#22263C", outline="")
            c.create_oval(eye_x - 3, top + 60, eye_x + 1, top + 65,
                          fill="#FFFFFF", outline="")
        c.create_oval(cx - 28, top + 74, cx - 14, top + 82,
                      fill=COLORS["blush"], outline="")
        c.create_oval(cx + 14, top + 74, cx + 28, top + 82,
                      fill=COLORS["blush"], outline="")
        c.create_arc(cx - 8, top + 76, cx + 8, top + 88, start=200, extent=140,
                     style="arc", outline="#8A6A5E", width=2)
        # 身体
        c.create_oval(cx - 40, top + 98, cx + 40, top + 152,
                      fill=COLORS["accent"], outline="")
        c.create_oval(cx - 14, top + 108, cx + 14, top + 130,
                      fill=COLORS["accent_soft"], outline="")

    def _breathe(self) -> None:
        if not self._alive:
            return
        # 24 帧 × 300ms ≈ 7 秒一个周期：慢到不抢注意力，又能看出在动
        self._phase = (self._phase + 1) % 24
        self._draw()
        self.after(300, self._breathe)

    def set_state(self, text: str, mood: str = "calm") -> None:
        """更新状态文字与情绪（mood=warm 时光晕转暖色）。"""
        self._state_text = str(text or "")
        self._mood = mood
        self._draw()

    def stop(self) -> None:
        self._alive = False


class StatusBar(tk.Frame):
    """顶部状态区：模型 / 记忆条数 / 服务健康，一眼看完。"""

    def __init__(self, master, **kw):
        super().__init__(master, bg=COLORS["panel"], **kw)
        self.items: dict[str, tk.Label] = {}
        for index, (key, text) in enumerate((("model", "模型：—"),
                                             ("memory", "记忆：—"),
                                             ("service", "服务：—"))):
            label = tk.Label(self, text=text, bg=COLORS["panel"],
                             fg=COLORS["text_dim"], font=FONT_SMALL)
            label.pack(side="left", padx=(12 if index == 0 else 18, 0), pady=6)
            self.items[key] = label

    def set(self, key: str, text: str, level: str = "dim") -> None:
        label = self.items.get(key)
        if not label:
            return
        color = {"ok": COLORS["ok"], "warn": COLORS["warn"],
                 "err": COLORS["err"]}.get(level, COLORS["text_dim"])
        label.configure(text=text, fg=color)


def demo(selftest: bool = False) -> None:
    root = tk.Tk()
    root.title("界面预览 · 未央")
    root.configure(bg=COLORS["bg"])
    root.geometry("760x560")
    pick_font(root)

    StatusBar(root).pack(fill="x")
    body = tk.Frame(root, bg=COLORS["bg"])
    body.pack(fill="both", expand=True)

    panel = CharacterPanel(body)
    panel.pack(side="left", fill="y", padx=(10, 6), pady=8)
    panel.set_state("在听你说")

    chat = tk.Frame(body, bg=COLORS["bg"])
    chat.pack(side="left", fill="both", expand=True, padx=(6, 10), pady=8)
    Bubble(chat, "早。今天想聊点什么？", mine=False).pack(fill="x", pady=3)
    Bubble(chat, "今天上班有点累。", mine=True).pack(fill="x", pady=3)
    Bubble(chat, _placeholder(), mine=False,
           on_replay=lambda: print("（示例：这里接回听）")).pack(fill="x", pady=3)
    TypingIndicator(chat).pack(anchor="w", padx=44, pady=4)

    bar = StatusBar(root)
    bar.set("model", "模型：weiyang-v1（8080）", "ok")
    bar.set("memory", "记忆：26 条", "dim")
    bar.set("service", "服务：对话 ✓ 嵌入 ✓", "ok")
    # selftest：构建完就关窗走人（冒烟测试用）；否则展示 1.4 秒
    root.after(300 if selftest else 1400, root.destroy)
    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="界面视觉底座预览")
    parser.add_argument("--selftest", action="store_true",
                        help="构建后自动退出（不开窗等待，用于冒烟测试）")
    args = parser.parse_args()
    demo(selftest=args.selftest)
    print("界面组件构建成功 ✓（预览窗口已展示配色/气泡/打字指示/角色区/状态区）")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
