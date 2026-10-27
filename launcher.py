"""模型启动器（可选模型、可调启动参数、可存预设）。

为什么要它：原来每个模型一个 .bat，改参数得去编辑批处理。现在统一成一个入口。

用法：
    python 启动模型.py                      # 交互式：选模型/预设 → 改参数 → 启动
    python 启动模型.py --list               # 只看有哪些模型和预设
    python 启动模型.py --preset 对话·GPT-5-Distill-Heretic
    python 启动模型.py --model Heretic --port 8080 --ctx 8192 --ngl 99
    python 启动模型.py --save 我的配置 --model Heretic --ctx 16384
    python 启动模型.py --default 我的配置   # 设为默认（交互式和启动界面都用它）
    python 启动模型.py --status             # 看正在跑的服务
    python 启动模型.py --stop 8080          # 停掉某个端口上的服务

启动后会一直停在这里看着服务（关掉窗口/按 Ctrl+C 会顺手把服务停掉，
不会留下后台残留进程）。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from services.manager import ServiceManager  # noqa: E402
from services.presets import PresetStore, clean  # noqa: E402

PARAM_HELP = {
    "port": "端口（对话默认 8080、嵌入默认 8081）",
    "ctx": "上下文长度（越大越吃显存）",
    "ngl": "放到 GPU 的层数（0=纯 CPU，99=全部）",
    "threads": "CPU 线程数",
    "kv": "KV 缓存量化（f16/q8_0/q4_0，越小越省显存）",
    "batch": "批大小",
    "ubatch": "微批大小",
    "parallel": "并行请求数",
    "extra": "额外参数（原样拼到命令行）",
}

# 给批处理用的英文别名（cmd 传中文参数在不同代码页下容易乱码，所以另给一套短名）
# 注意：别名**直接按模型关键字与端口来**，不依赖预设——预设全由使用者自己加，
# 批处理必须在这之前就能用。
ALIASES = {
    "baseline": {"model": "Qwen3-4B", "port": 8080},
    "heretic": {"model": "Heretic", "port": 8080},
    "weiyang": {"model": "weiyang", "port": 8080},
    "cpu": {"model": "Qwen3-4B", "port": 8080, "ngl": 0},
    "embed": {"model": "bge", "port": 8081, "kind": "embed"},
}


def show_list(models: list[dict], store: PresetStore) -> None:
    print("=" * 66)
    print("可用模型（扫到的文件）")
    print("=" * 66)
    for index, item in enumerate(models, 1):
        kind = "对话" if item.get("kind") == "chat" else "嵌入"
        size = item.get("size_gb") or 0
        print(f"  {index:>2}. [{kind}] {item['name']}　{size:.2f} GB")
        print(f"      {item['path']}")
    print()
    print("=" * 66)
    print("已保存的预设")
    print("=" * 66)
    default_name, _ = store.default()
    for name in store.names():
        spec = store.get(name)
        mark = "（默认）" if name == default_name else ""
        print(f"  · {name}{mark}")
        print(f"      模型 {pathlib.Path(str(spec.get('model', ''))).name}")
        print(f"      端口 {spec.get('port')}　ctx {spec.get('ctx')}　ngl {spec.get('ngl')}"
              f"　线程 {spec.get('threads')}　KV {spec.get('kv')}")
    print()


def ask(prompt: str, default: str = "") -> str:
    tip = f"（回车用 {default}）" if default != "" else ""
    try:
        got = input(f"{prompt}{tip}：").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return got or default


def edit_params(spec: dict) -> dict:
    """逐项改参数，回车就保持原值。"""
    print("\n现在可以改启动参数（不填就直接回车保持原值）：")
    for key, help_text in PARAM_HELP.items():
        current = spec.get(key, "")
        value = ask(f"  {key}　{help_text}", str(current))
        if value != "":
            spec[key] = value
    return clean(spec)


HELP_TEXT = """可用指令（命令行）
  查看
    --list                     看可用模型与预设
    --list-presets             只看预设清单
    --status                   看正在跑的服务
    --help                     显示这份指令列表
  启动
    --preset 名字              用某个预设启动
    --alias baseline|heretic|weiyang|cpu|embed
                               批处理用的短名（不依赖预设）
    --model 关键字             按模型名关键字启动（套默认参数）
    --port/--ctx/--ngl/--threads/--kv/--batch/--ubatch/--parallel/--extra
                               临时覆盖启动参数
    --save 名字 [参数...]      把当前配置存成预设（只保存，不启动）
    --default 名字             把某个预设设为默认
    --delete-preset 名字       删除某个预设
  停止
    --stop 端口                停掉某个端口上的服务

交互里可输入（双击 bat 就是交互模式）
    p3 或 预设3                用第 3 个预设
    2                          用第 2 个模型（套默认参数）
    d2                         删除第 2 个预设
    h                          显示这份指令
    0 或 q                     取消退出
    直接回车                    用默认预设
"""


def pick_target(models: list[dict], store: PresetStore) -> tuple[dict, str] | None:
    """让用户选**预设**或**模型**，也可以在这里删预设、看指令。

    之前只能选模型（或回车用默认预设），没法主动挑某个预设——这里补上：
        直接回车      → 用默认预设
        p2 / 预设2    → 用第 2 个预设（保留它已存的参数）
        3             → 用第 3 个模型（套默认参数）
        d2            → 删除第 2 个预设（会再问一次确认）
        h             → 显示指令列表
        q / 0         → 取消
    """
    while True:
        names = store.names()
        print("\n选预设（保留它存好的参数）或直接选模型（套默认参数）：")
        for index, name in enumerate(names, 1):
            spec = store.get(name)
            default_mark = "（默认）" if name == store.default()[0] else ""
            print(f"  p{index}. 预设 {name}{default_mark}　端口 {spec.get('port')}"
                  f"　ctx {spec.get('ctx')}　ngl {spec.get('ngl')}")
        if not names:
            print("  （一个预设都没有：输入模型序号启动；或用 --save 名字 添加预设）")
        print("  0. 取消　　输入 h 看指令、d<序号> 删除预设")
        raw = ask("输入 p<序号> 用预设、或直接输模型序号", "回车＝默认预设").strip().lower()

        if raw in ("h", "help", "?", "？"):
            print(HELP_TEXT)
            continue
        if raw in ("q", "quit", "0"):
            return None
        if raw.startswith(("d", "删")) and raw.lstrip("d删").strip().isdigit():
            index = int(raw.lstrip("d删").strip())
            if not (1 <= index <= len(names)):
                print("预设序号超出范围")
                continue
            name = names[index - 1]
            if ask(f"确认删除预设「{name}」？(y/N)", "n").lower().startswith("y"):
                store.delete(name)
                if store.default()[0] == name and store.names():
                    store.set_default(store.names()[0])
                    print(f"默认预设改成了：{store.names()[0]}")
                print(f"已删除预设：{name}（还剩 {len(store.names())} 个）")
            else:
                print("已取消删除")
            continue
        if raw in ("", "default"):
            name, spec = store.default()
            if not name:
                print("还没有默认预设，且一个预设都没有——请输入模型序号。")
                continue
            return clean(spec), f"默认预设「{name}」"

        number = raw.lstrip("p预设").strip()
        if raw.startswith(("p", "预设")) and number.isdigit():
            index = int(number)
            if 1 <= index <= len(names):
                name = names[index - 1]
                return clean(store.get(name)), f"预设「{name}」"
            print("预设序号超出范围")
            continue

        if number.isdigit():
            index = int(number)
            if 1 <= index <= len(models):
                chosen = models[index - 1]
                from services.manager import CHAT_DEFAULTS, EMBED_DEFAULTS
                kind = chosen.get("kind") or "chat"
                spec = clean({**(EMBED_DEFAULTS if kind == "embed" else CHAT_DEFAULTS),
                              "kind": kind, "model": chosen["path"]})
                return spec, f"模型 {chosen['name']}（默认参数）"
            print("模型序号超出范围")
            continue

        print("没看懂这个输入，输入 h 看指令。")


def pick_model(models: list[dict]) -> dict | None:
    """（旧接口，保留兼容）只选模型。"""
    raw = ask("\n选哪个模型（输入序号，直接回车＝用默认预设）")
    if not raw.isdigit():
        return None
    index = int(raw)
    if not (1 <= index <= len(models)):
        print("序号超出范围")
        return None
    chosen = models[index - 1]
    spec = {"kind": chosen.get("kind") or "chat", "model": chosen["path"]}
    from services.manager import CHAT_DEFAULTS, EMBED_DEFAULTS
    spec = {**(EMBED_DEFAULTS if spec["kind"] == "embed" else CHAT_DEFAULTS), **spec}
    return clean(spec)


def watch(manager: ServiceManager, ports: list[int]) -> None:
    """启动后停在这里看服务；Ctrl+C 时把服务停掉，不留残留进程。

    只在**状态发生变化**时打印，避免每 5 秒刷一行同样的内容。
    """
    print("\n服务已启动。状态有变化时这里会提示；按 Ctrl+C 停止（关掉窗口也会一起停）。")
    seen: dict[int, tuple] = {}
    try:
        while True:
            time.sleep(5)
            for item in manager.status(ports):
                state = "健康" if item.get("healthy") else "未响应"
                model = str(item.get("model") or "").replace("\\", "/").split("/")[-1]
                mark = (state, item.get("pid"), model)
                if seen.get(item["port"]) == mark:
                    continue
                seen[item["port"]] = mark
                print(f"  [{time.strftime('%H:%M:%S')}] 端口 {item['port']}：{state}"
                      f"　pid={item.get('pid') or '-'}　{model}")
    except KeyboardInterrupt:
        print("\n收到停止信号，正在关闭服务…")
        for port in ports:
            if manager.stop(port):
                print(f"  已停止 {port}")
        print("已退出。")


def main() -> int:
    parser = argparse.ArgumentParser(description="模型启动器：选模型、调参数、存预设")
    parser.add_argument("--list", action="store_true", help="列出模型与预设")
    parser.add_argument("--preset", default="", help="用某个预设启动")
    parser.add_argument("--alias", default="", choices=["", *ALIASES],
                        help="预设别名：baseline / heretic / cpu / embed（批处理用）")
    parser.add_argument("--model", default="", help="按名字关键字选模型")
    parser.add_argument("--kind", default="", choices=["", "chat", "embed"],
                        help="模型类型（不指定就按文件自动判断）")
    parser.add_argument("--port", type=int)
    parser.add_argument("--ctx", type=int)
    parser.add_argument("--ngl", type=int)
    parser.add_argument("--threads", type=int)
    parser.add_argument("--kv", default="")
    parser.add_argument("--batch", type=int)
    parser.add_argument("--ubatch", type=int)
    parser.add_argument("--parallel", type=int)
    parser.add_argument("--extra", default="")
    parser.add_argument("--save", default="", help="把这套配置存成预设")
    parser.add_argument("--default", default="", help="把某个预设设为默认")
    parser.add_argument("--delete-preset", default="", help="删除某个预设")
    parser.add_argument("--list-presets", action="store_true", help="只看预设清单")
    parser.add_argument("--status", action="store_true", help="看服务状态")
    parser.add_argument("--stop", type=int, help="停掉某个端口上的服务")
    parser.add_argument("--yes", action="store_true", help="不问确认，直接启动")
    parser.add_argument("--serve", action="store_true",
                        help="同时起对外接口（桌宠用，OpenAI 兼容，端口见 --serve-port）")
    parser.add_argument("--serve-foreground", action="store_true",
                        help="只起对外接口并占住前台（Ctrl+C 退）")
    parser.add_argument("--serve-port", type=int, default=8082,
                        help="对外接口端口（默认 8082；8080/8081 被模型占用）")
    args = parser.parse_args()

    # 只起接口：不碰模型
    if args.serve_foreground and args.serve:
        print("--serve-foreground 和 --serve 选一个就行：前者占前台，后者跟模型一起后台跑")
        return 2
    if args.serve_foreground:
        from pet_server import DEFAULT_PORT, main as serve_main
        port = args.serve_port or DEFAULT_PORT
        print(f"只起对外接口：http://127.0.0.1:{port}")
        print("（对话模型需要你自己在另一个窗口起：python launcher.py --alias baseline）")
        return serve_main(["--port", str(port)])

    manager = ServiceManager()
    store = PresetStore()
    models = manager.models()

    if args.list:
        show_list(models, store)
        return 0

    if args.list_presets:
        if not store.names():
            print("（一个预设都没有；用 --save 名字 添加）")
        default_name, _ = store.default()
        for index, name in enumerate(store.names(), 1):
            spec = store.get(name)
            mark = "（默认）" if name == default_name else ""
            print(f"  p{index}. {name}{mark}　模型 "
                  f"{pathlib.Path(str(spec.get('model', ''))).name}"
                  f"　端口 {spec.get('port')}　ctx {spec.get('ctx')}"
                  f"　ngl {spec.get('ngl')}　线程 {spec.get('threads')}"
                  f"　KV {spec.get('kv')}")
        return 0

    if args.delete_preset:
        name = args.delete_preset
        if name not in store.names():
            print(f"没有这个预设：{name}")
            return 1
        store.delete(name)
        if store.default()[0] == name:
            rest = store.names()
            if rest:
                store.set_default(rest[0])
                print(f"默认预设改成了：{rest[0]}")
        print(f"已删除预设：{name}（还剩 {len(store.names())} 个）")
        return 0

    if args.default:
        if args.default in store.names():
            store.set_default(args.default)
            print(f"默认预设已设为：{args.default}")
        else:
            print(f"没有这个预设：{args.default}")
        return 0

    if args.status:
        for item in manager.status():
            state = "健康" if item.get("healthy") else "未响应"
            print(f"  [{item['port']}] {state}　pid={item.get('pid') or '-'}"
                  f"　{item.get('model') or ''}")
        return 0

    if args.stop:
        print("已停止" if manager.stop(args.stop) else "该端口上没有在跑的服务")
        return 0

    # ---- 决定要启动的配置（别名 > 预设 > 模型 > 默认预设 > 交互）----
    spec: dict = {}
    if args.alias:
        # 别名按"模型关键字 + 端口"解析，不依赖预设是否存在
        alias_spec = ALIASES.get(args.alias, {})
        found = manager.find_model(str(alias_spec.get("model") or ""))
        if not found:
            print(f"别名 {args.alias} 对应的模型没找到（关键字 "
                  f"「{alias_spec.get('model')}」），用 --list 看看有哪些")
            return 1
        from services.manager import CHAT_DEFAULTS, EMBED_DEFAULTS
        alias_kind = alias_spec.get("kind") or found.get("kind") or "chat"
        spec = clean({**(EMBED_DEFAULTS if alias_kind == "embed" else CHAT_DEFAULTS),
                      "kind": alias_kind, "model": found["path"],
                      "port": alias_spec.get("port", 8080)})
        if "ngl" in alias_spec:
            spec["ngl"] = alias_spec["ngl"]
        print(f"用模型：{found['name']}（别名 {args.alias}）")
    elif args.preset:
        if args.preset not in store.names():
            print(f"没有这个预设：{args.preset}"
                  + (f"（可用：{'、'.join(store.names())}）" if store.names()
                     else "（现在一个预设都没有，用 --save 名字 添加）"))
            return 1
        spec = clean(store.get(args.preset))
        print(f"用预设：{args.preset}")
    elif args.model:
        found = manager.find_model(args.model)
        if not found:
            print(f"没找到名字里含「{args.model}」的模型，用 --list 看看有哪些")
            return 1
        from services.manager import CHAT_DEFAULTS, EMBED_DEFAULTS
        kind = args.kind or found.get("kind") or "chat"
        spec = clean({**(EMBED_DEFAULTS if kind == "embed" else CHAT_DEFAULTS),
                      "kind": kind, "model": found["path"]})
        print(f"用模型：{found['name']}")
    elif not sys.stdin.isatty():
        name, spec = store.default()
        print(f"非交互模式，使用默认预设：{name}")
    else:
        show_list(models, store)
        picked = pick_target(models, store)
        if not picked:
            print("已取消。")
            return 0
        spec, label = picked
        print(f"已选：{label}")

    # ---- 命令行参数覆盖 ----
    for key in ("port", "ctx", "ngl", "threads", "kv", "batch", "ubatch", "parallel",
                "extra"):
        value = getattr(args, key, None)
        if value not in (None, ""):
            spec[key] = value
    if args.kind:
        spec["kind"] = args.kind
    spec = clean(spec)

    # ---- 交互式改参数 ----
    if not (args.preset or args.model) and sys.stdin.isatty() and not args.yes:
        if ask("\n要改启动参数吗？(y/N)", "n").lower().startswith("y"):
            spec = edit_params(spec)
        save_name = ask("要把这套配置存成预设吗？（不存就直接回车）")
        if save_name:
            store.upsert(save_name, spec)
            print(f"已保存预设：{save_name}")

    if args.save:
        store.upsert(args.save, spec)
        print(f"已保存预设：{args.save}")
        # 定义预设时就到此为止（不再去启动服务）——要启动用 --preset 名字
        if args.model or args.preset or args.alias:
            print(f"下次启动：python launcher.py --preset {args.save}")
            return 0

    # ---- 启动 ----
    port = int(spec.get("port") or 8080)
    if not manager.port_free("127.0.0.1", port):
        owner = manager.port_owner(port)
        print(f"！端口 {port} 已被占用（pid={owner}）——换端口，或用 --stop {port} 停掉它")
        return 1
    print(f"\n启动中：{pathlib.Path(str(spec.get('model',''))).name}")
    print("命令：" + " ".join(manager.build_command(spec)))
    try:
        handle = manager.start(spec, wait=True)
    except Exception as exc:
        print(f"！启动失败：{exc}")
        return 1
    print(f"已启动（pid={handle.get('pid')}），健康检查："
          f"{'通过' if handle.get('healthy') else '未通过'}")
    if not handle.get("healthy"):
        print(manager.diagnose(port))
        return 1

    # 模型起来了，顺便把对外接口也起上（后台线程，跟 launcher 一起活）
    if args.serve:
        from pet_server import ChatService, start_background
        try:
            start_background(port=args.serve_port, service=ChatService())
            print(f"对外接口已就绪：http://127.0.0.1:{args.serve_port}"
                  "（OpenAI 兼容，桌宠可直接连）")
        except OSError as exc:
            print(f"！对外接口没起来（端口被占？）：{exc}")

    watch(manager, [port])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
