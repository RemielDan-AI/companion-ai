"""界面引用的方法/属性**必须真的存在**——防"静默删除"这类事故。

## 为什么需要这个

本项目已经**两次**因为"替换一整段时把夹在中间的方法一起删掉"而出事：

  ① `services/models_config.set_role_param` 被删 → 界面改模型"显示保存成功"，
     **实际一次都没写盘**。
  ② `gui_settings.SettingsWindow._toggle_switch` 被删 → 点任何勾选框，
     **终端一片红（AttributeError），设置一项都没保存**，勾选框还会自己弹回去。

两次都是"删的时候没人发现"——因为静态检查不跑 GUI，而冒烟测试只覆盖了部分路径。
所以这里做一个**静态**检查：把 GUI 模块里所有 `self.xxx(` 调用和 `self.xxx`
属性访问收集起来，逐一对一下类里到底有没有定义。

⚠ 这不能替代真跑界面（冒烟测试仍然要跑），但能在**秒级**挡住"方法凭空消失"。
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAIL: list[str] = []

# 这些是 Tk 框架自带的属性/方法，类里不需要定义
TK_BUILTINS = {
    "tk", "master", "winfo_children", "winfo_exists", "winfo_width", "winfo_height",
    "winfo_ismapped", "winfo_reqwidth", "winfo_reqheight", "winfo_rootx", "winfo_rooty",
    "winfo_id", "winfo_geometry", "winfo_screenwidth", "winfo_screenheight",
    "winfo_toplevel", "configure", "cget", "destroy", "pack", "grid", "place",
    "pack_forget", "grid_remove", "grid_forget", "pack_propagate", "after",
    "after_cancel", "bind", "bind_all", "unbind_all", "update", "update_idletasks",
    "title", "geometry", "minsize", "transient", "lift", "attributes", "protocol",
    "mainloop", "quit", "wait_window", "clipboard_clear", "clipboard_append",
    "focus_set", "focus_get", "grab_set", "grab_release", "bell", "iconify",
    "deiconify", "state", "withdraw", "wm_attributes", "tk_focusNext",
    "columnconfigure", "rowconfigure", "grid_info", "pack_info", "nametowidget",
    "register", "selection_clear", "bar", "index", "see", "insert", "delete",
    "cc", "call", "winfo_pixels",
    # 动态 setattr 出来的（本测试不做数据流分析，避免误报一整片）
    "_save_ui_state", "auto_speak_var", "speak_lang_var",
}


def collect_self_refs(tree: ast.AST) -> set[str]:
    """收集 `self.xxx` 的名字（调用和方法访问都算）。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            names.add(node.attr)
    return names


def _attr_names(target: ast.AST) -> set[str]:
    """从一个赋值目标里挖出所有 `self.x` 的 x。

    ⚠ **必须处理元组解包**：`self._art_w, self._art_h = width, height`
      的 target 是 `ast.Tuple` 而不是 `ast.Attribute`——
      第一版没处理，把 `_art_w`/`_art_h` 报成"未定义"（误报）。
    """
    out: set[str] = set()
    if isinstance(target, ast.Attribute):
        if isinstance(target.value, ast.Name) and target.value.id == "self":
            out.add(target.attr)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            out |= _attr_names(item)
    return out


def collect_defs(tree: ast.AST) -> set[str]:
    """类里定义的方法名 + 实例属性名（`self.x = ...` 也算定义了）。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
        # self.x = ... / self.a, self.b = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                out |= _attr_names(target)
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Attribute) and \
                    isinstance(target.value, ast.Name) and target.value.id == "self":
                out.add(target.attr)
    return out


def check_module(path: pathlib.Path, class_names: list[str]) -> None:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in class_names:
            continue
        defined = collect_defs(node)
        # 类属性也算
        used = collect_self_refs(node)
        missing = sorted(n for n in used
                         if n not in defined and n not in TK_BUILTINS)
        label = f"{path.name}::{node.name}"
        if missing:
            FAIL.append(f"{label} 缺 {missing}")
            print(f"  ✗ {label}")
            for m in missing:
                print(f"      **self.{m} 被引用但类里没有定义**")
        else:
            print(f"  ✅ {label}：{len(used)} 处 self.xxx 引用全部有定义")


def main() -> int:
    print("=" * 88)
    print("界面接线：self.xxx 被引用就必须有定义")
    print("=" * 88)

    # ⚠ 只检查**自己写的顶层窗口类**，不碰第三方/Tk 组件
    targets = [
        (ROOT / "gui_settings.py", ["SettingsWindow"]),
        (ROOT / "gui.py", ["Window"]),
        (ROOT / "gui_theme.py", ["CharacterPanel", "StatusBar", "ScrollableRow"]),
    ]
    for path, classes in targets:
        if not path.exists():
            FAIL.append(f"{path.name} 不存在")
            continue
        try:
            names = [n.name for n in ast.parse(path.read_text(encoding="utf-8")).body
                     if isinstance(n, ast.ClassDef)]
        except Exception as exc:
            FAIL.append(f"{path.name} 解析失败：{exc}")
            continue
        exist = [c for c in classes if c in names]
        if not exist:
            print(f"  ⚠ {path.name} 里没找到 {classes}（可能改名了）")
            continue
        for cls in exist:
            check_module(path, [cls])

    # 关键方法点名在（防止被"整段替换"顺手删掉）
    print()
    import importlib
    for mod_name, cls_name, methods in (
            ("gui_settings", "SettingsWindow",
             ("_toggle_switch", "_toggle_source", "_save_roles", "_build_model_roles",
              "_save_api_switches", "_load_api_switches", "_refresh_model_roles",
              "_apply_model_roles", "_start_all_models", "_stop_all_models",
              "_start_think")),
    ):
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, cls_name, None)
        for m in methods:
            ok = cls is not None and hasattr(cls, m)
            print(f"  {'✅' if ok else '✗'} {cls_name}.{m} 存在")
            if not ok:
                FAIL.append(f"{cls_name}.{m} 缺失")

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
