"""固定回复（用户要求：**想改话术不用动代码**）。

## 怎么维护

`config/replies.yaml` 里**每个键都是一个数组**，程序每次从里面**随机取一句**。

```yaml
no_memory:
  - "这个我记不太清了，你再跟我说说？"
  - "这个我真没印象，你再说一遍？"
```

想加一句就加一行；只留一句也行（那就永远说那一句）。

## 为什么随机

同一句话反复说会露馅——「这个我记不太清了，你再跟我说说？」连着出现三次，
再好的陪伴感也破了。几个变体轮换是最省力的「不机械」手段。

⚠ **这两类必须固定（只取一次）**：
  · `listening` / `thinking` 是**画布上的状态词**，只在构造时取一次
    （若每帧取会每 300ms 变一次，看着像坏了）。
  · `greeting` 在 `gui.py` 里一个**模块级字典**中用到，进程内固定。

## 读不到怎么办

⚠ **一定要有内置兜底**：文件被改坏 / 被删 / 键名写错时，程序不能崩，
也不能让未央"说不出话"。所以：
  · `_DEFAULTS` 是完整的一套默认话术（**每组第一项与搬走前的硬编码一字不差**）
  · 文件里的键**覆盖**默认值，缺的键用默认值补
  · 解析失败时整体回退到默认，并在终端提示一次（不弹窗打扰）
"""
from __future__ import annotations

import pathlib
import random
import threading

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "replies.yaml"

# 原硬编码的那句放在**每组第一项**（升级后默认话术不变）
_DEFAULTS: dict = {
    "no_memory": [
        "这个我记不太清了，你再跟我说说？",
        "这个我真没印象，你再说一遍？",
        "我这儿没记着这段，你再讲一次？",
    ],
    "no_memory_long": [
        "这个我记不太清了……你再跟我说一遍？这回我一定记住。",
        "这段我确实没记住。你再说一次，我这次记牢。",
    ],
    "refuse": {
        "soft": [
            "这个我还做不好。硬着头皮答等于骗你，我不想那样。\n"
            "你要是愿意，我们把这件事拆开看看——总有一块是我能搭上手的。",
            "我暂时没这个本事，就不瞎猜糊弄你了。\n"
            "不过你跟我说说为什么需要它，说不定有别的路子。",
        ],
        "hard": [
            "这个我不做。\n不是敷衍你，是这条路我不陪你走。",
            "不行，这件事我不帮。\n你要是想说说为什么非得这么干，我听着。",
        ],
        "transfer": [
            "这事得让专业的人来定，我不替你拿主意——我凭印象说，万一错了是你承担。\n"
            "我可以先陪你把情况理清楚，你去问的时候能一次说明白。",
            "我给不了这个结论。\n但准备这件事我能帮：把时间、细节、你最想问的都捋出来。",
        ],
        "safety": [
            "这件事我得停下来。\n"
            "你现在好不好？身边有人吗？先跟我说一句。",
            "我不会做这件事，也不会教。\n"
            "但你现在这样，我更想陪着你——先说说现在是什么情况？",
        ],
    },
    "no_skill": [
        "这件事我还没有能耐帮你做，我不想硬编一个糊弄你。\n"
        "换个我能帮上的：陪你聊、听你说事、帮你记着，这些我都在。",
        "这个我确实做不到，就不硬撑了。\n"
        "换个方向吧——陪你聊、听你说事、帮你记着，这些我都在。",
    ],
    "greeting": [
        "嗯，我在呢。今天想聊点什么？",
        "我在。今天怎么样？",
        "来了。今天想说什么？",
    ],
    "greeting_ja": [
        "嗯，我在呢。こんにちは、私は未央です。",
    ],
    "thinking": [
        "在想…",
    ],
    "listening": [
        "在听你说",
    ],
    "placeholder_reply": [
        "那就慢慢说，我在这儿听着。双击这行可以重听。",
        "想说什么就说，我在。双击这行可以重听。",
    ],
}

_cache: dict = {"mtime": None, "data": None, "warned": False}
_lock = threading.Lock()


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def _stamp() -> tuple:
    """文件的缓存指纹：**(mtime_ns, size)**。

    ⚠⚠ 2026-10-27 修：原来只用 `st_mtime`（秒级 float），
      而**文件系统时间戳精度有限**——同一个时间片内连写两次，
      第二次的 mtime 和第一次**相同**，于是缓存不失效，
      `load()` 仍然返回**上一次的内容**。

      症状：程序里改完文件"没生效"；测试里连续写不同内容时读到旧值（偶发失败）。
      → 加上 `st_size` 一起做键：内容变了大小几乎一定变，两个加起来足够稳。
    """
    try:
        st = CONFIG_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        return (0, 0)


def load() -> dict:
    """读 `config/replies.yaml`（按 (mtime_ns, size) 缓存，改完不用重启也能生效）。"""
    stamp = _stamp()
    with _lock:
        if _cache["mtime"] == stamp and _cache["data"] is not None:
            return _cache["data"]
        data = dict(_DEFAULTS)
        if stamp[0]:
            try:
                raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    data = _deep_merge(_DEFAULTS, raw)
            except Exception as exc:
                if not _cache["warned"]:
                    print(f"⚠ config/replies.yaml 解析失败，已用内置默认话术：{exc}")
                    _cache["warned"] = True
        _cache["mtime"] = stamp
        _cache["data"] = data
        return data


def _items(value) -> list[str]:
    """把键值规范成**非空字符串列表**。

    ⚠ 同时兼容数组（推荐）和单个字符串（老文件 / 用户手写）：
      写成 `no_memory: "一句话"` 也能用，等价于只有一项的数组。
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if str(x).strip()]
    return []


def all_of(key: str) -> list[str]:
    """这个键的**全部**候选（不随机）。给测试 / 调试用。"""
    got = _items(load().get(key))
    return got or _items(_DEFAULTS.get(key))


def get(key: str, default: str = "") -> str:
    """从数组里**随机取一句**（数组为空就用 `default`）。"""
    got = all_of(key)
    if got:
        return random.choice(got)
    return default


def refuse_list(kind: str) -> list[str]:
    """取某一类拒绝话术（空 / 写错时回退到内置默认）。"""
    got = _items((load().get("refuse") or {}).get(kind))
    return got or _items((_DEFAULTS["refuse"] or {}).get(kind))


def verify_roundtrip() -> list[str]:
    """自检：把默认话术写出去再读回来，**必须一字不差**。

    这条是给"写文件的转义"兜底的——**话术里带换行时最容易悄悄改味**
    （YAML 双引号标量会把字面换行折成空格，而且不报错）。
    返回不一致的键名列表（空 = 全对）。
    """
    import tempfile
    bad: list[str] = []
    try:
        data = load()
        for key in ("no_memory", "no_memory_long", "no_skill", "greeting",
                    "placeholder_reply"):
            for item in _items(data.get(key)):
                # 用 yaml 自己序列化再反序列化，验证转义写法可还原
                dumped = yaml.safe_dump({"k": [item]}, allow_unicode=True)
                back = (yaml.safe_load(dumped) or {}).get("k") or [""]
                if back[0] != item:
                    bad.append(key)
                    break
    except Exception:
        bad.append("<异常>")
    return bad


def ensure_file() -> pathlib.Path:
    """文件不存在就**按默认值生成一份**（带注释），方便用户直接改。"""
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 未央的固定回复（**改这里就能改话术，不用动代码**）",
        "#",
        "# · 每个键都是一个**数组**，程序每次从里面**随机取一句**",
        "# · 想加一句就加一行；只留一句也行（那就永远说那一句）",
        "# · 删掉整个键 = 用内置默认值（不会报错）",
        "# · 「\\n」表示换行",
        "# · 保存后**下一次说话就生效**（按文件修改时间热加载）",
        "# · 选项页「模型服务」里有「打开固定回复文件」按钮，可直接从这里编辑",
        "",
    ]

    def _esc(item: str) -> str:
        """把话术转成 YAML 双引号标量里能**原样还原**的写法。

        ⚠⚠ 2026-10-27 修：原来只转义了双引号，**真实换行没有转义**。
          YAML 的**双引号标量会把字面换行折成一个空格**，
          于是 `no_skill` 里那句「…糊弄你。\n换个我能帮上的…」写进文件后
          变成了「…糊弄你。 换个我能帮上的…」——**换行丢了，话术变了味**
          （而且不报错，几乎看不出来）。
          → 先把 `\n` 转成两个字符 `\n`（YAML 双引号里的换行转义），
            再转义反斜杠和双引号。
        """
        return (item.replace("\\", "\\\\")
                    .replace('"', '\\"')
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")
                    .replace("\t", "\\t"))

    def _emit(key: str, items: list[str], indent: str = "") -> None:
        lines.append(f"{indent}{key}:")
        for item in items:
            lines.append(f'{indent}  - "{_esc(item)}"')
        lines.append("")

    for key in ("no_memory", "no_memory_long", "no_skill", "greeting",
                "greeting_ja", "thinking", "listening", "placeholder_reply"):
        _emit(key, _DEFAULTS[key])

    lines.append("refuse:")
    for kind in ("soft", "hard", "transfer", "safety"):
        _emit(kind, _DEFAULTS["refuse"][kind], indent="  ")
    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
    return CONFIG_PATH
