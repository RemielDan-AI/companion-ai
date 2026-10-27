"""验证主窗口布局：**每个功能按钮都完整落在窗口内**（不信截图，信几何）。

## 为什么要有这个

改布局时靠截图判断会骗人：`ImageGrab` 在缩放/遮挡/高 DPI 下截出来的区域
和控件树的真实位置可能对不上（本项目实测就遇到过——截图显示输入框消失，
但 `winfo_ismapped()=1`、y 坐标也正常）。

而且更根本的一类 bug 截图只能"看到"不能"证明"：
`pack_propagate(False)` 之后子控件**溢出会被直接裁掉**（不是压扁），
窗口一矮，最后几行按钮就无声消失——**切掉的按钮等于不存在**。

所以这里直接量：按钮的实际 `y + height` 必须 ≤ 窗口高度。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tkinter as tk                      # noqa: E402
from tkinter import ttk                   # noqa: E402

import gui                                # noqa: E402
from app import Companion                 # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def all_buttons(widget) -> list:
    out = []
    for child in widget.winfo_children():
        if isinstance(child, (ttk.Button, tk.Button)):
            out.append(child)
        out.extend(all_buttons(child))
    return out


def main() -> int:
    print("=" * 88)
    print("主窗口布局：功能按钮必须完整可见")
    print("=" * 88)

    tmp = ROOT / "_shot_tmp"
    tmp.mkdir(exist_ok=True)
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    comp = Companion(use_memory=True, session="layout",
                     memory_path=str(tmp / "m.db"),
                     index_path=str(tmp / "i.json"),
                     no_extract_service=True)
    window = gui.Window(root, companion=comp)
    root.geometry("1000x730+60+40")
    root.update()
    root.update_idletasks()
    root.update()

    win_h = root.winfo_height()
    win_w = root.winfo_width()
    top_y = root.winfo_rooty()
    print(f"  窗口 {win_w}x{win_h}")

    # ① 核心控件都在
    for name in ("entry", "send_button", "chat", "character", "status_var"):
        got = getattr(window, name, None)
        check(got is not None, f"self.{name} 存在")

    # ② 输入框可见且完整在窗口内
    entry = window.entry
    check(entry.winfo_ismapped() == 1, "输入框已布局（mapped）")
    bottom = entry.winfo_rooty() + entry.winfo_height() - top_y
    check(bottom <= win_h, "输入框完整落在窗口内",
          f"底边 {bottom} ≤ {win_h}")

    # ③ **每一个按钮**都不能超出窗口底部（这就是"切掉"的判据）
    buttons = all_buttons(root)
    overflow = []
    for btn in buttons:
        try:
            text = btn.cget("text")
        except Exception:
            text = "?"
        if not btn.winfo_ismapped():
            continue
        b = btn.winfo_rooty() + btn.winfo_height() - top_y
        if b > win_h:
            overflow.append((text, b))
    check(not overflow,
          f"**{len(buttons)} 个按钮全部在窗口内**",
          "超出：" + ", ".join(f"{t}({b}px)" for t, b in overflow[:6])
          if overflow else f"窗口高 {win_h}")

    # ④ 功能按钮一个都不能少（按名字点名，防止被悄悄删掉）
    texts = set()
    for btn in buttons:
        try:
            texts.add(str(btn.cget("text")).strip())
        except Exception:
            pass
    for want in ("⚙ 设置", "记忆", "技能", "价值观", "清空上下文",
                 "📚 记忆管理", "⚙ 模型服务", "🎭 AI 档案", "🔧 工具调用",
                 "👍 好", "✏ 纠错", "发送"):
        check(any(want in t for t in texts), f"按钮「{want}」在")

    # ⑤ 形象区确实加载了图（不是退化成自绘鲸鱼娘）
    ch = window.character
    if ch is not None:
        # ⚠ 公开仓库**不含立绘**（非本人作品、无再分发授权）。
        #   没有图时界面走自绘兜底形象，这**是设计好的行为**，不是失败。
        #   （第一版硬断言"必须加载到图"，别人 clone 下来直接一片红。）
        _has_asset = getattr(ch, "_art", None) is not None
        if _has_asset:
            check(True, "**形象图已加载**（不是自绘兜底）")
        else:
            print("  ⏭ 跳过「形象图已加载」：仓库内无立绘，走自绘兜底形象")
        from gui_theme import _find_character_asset
        asset = _find_character_asset()
        # ⚠ 公开仓库里**不含立绘**（非本人作品、无再分发授权），
        #   所以这里不能要求「一定用的是某张特定图」。
        #   有图 → 必须真的加载到它；没图 → 自绘兜底形象也算通过。
        if asset:
            # 不写死文件名：断言「配了图就真的加载到了配置里那张」
            import yaml as _yaml
            try:
                _cfg = _yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent
                                        / "config" / "gui.yaml").read_text(
                                            encoding="utf-8")) or {}
                _want = str(_cfg.get("character_asset") or "")
            except Exception:
                _want = ""
            check(not _want or pathlib.Path(asset).name == pathlib.Path(_want).name,
                  "用的是用户放的图", pathlib.Path(asset).name)
        else:
            print("  ⏭ 跳过「用的是用户放的图」：仓库内无立绘，"
                  "界面会走自绘兜底形象")
        if _has_asset:
            size = getattr(ch, "_art_size", (0, 0))
            check(size[0] > 80 and size[1] > 80, "形象尺寸合理", str(size))
        else:
            print("  ⏭ 跳过「形象尺寸合理」：没有立绘，尺寸由自绘逻辑决定")

    root.destroy()
    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
