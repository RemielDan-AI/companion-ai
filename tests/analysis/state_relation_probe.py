"""诊断：状态句（X 对 Y 过敏 / X 喜欢 Y / X 住在 Y）被抽成了什么。

## 为什么要单独看

能力上限探针里，**单跳基线「我妹妹对什么过敏」三次全败**，
而注入块里**没有「花生」**。查库发现：

    [relation] 对方提到的人：林小雨（过敏）      ← 过敏原「花生」没了

抽取把「林小雨**对花生**过敏」当成了**关系**，关系词写成「过敏」，
**宾语整个丢掉**。而关系本该是**角色**（妹妹/同事/宠物/单位），
「过敏」是**状态**——状态句是**事实**。

本探针把几种状态句的原始 JSON 打出来，确认是普遍现象还是个例。
（判据写在本文件里，不依赖 extract.py，这样它也能当"改之前"的基线。）

用法：
    python tests/model_server.py run --model v1.3 --ngl 15 \
        --script tests/analysis/state_relation_probe.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

# 状态/谓语词：出现在 `名字（X）` 的括号里就说明**分类错了**。
# ⚠ 判据要用 `extract._is_state_word`（权威版），别在这里另写一套：
#   本文件第一版自己写了个粗糙版，把 `南京（住的地方）`「广州（上学地点）」
#   这种**正当的地点角色**也报成"状态词当关系"——**探针判据比修复判据还粗**，
#   报出来的结论就是错的。没有就退回本地这份粗略版（保证能跑）。
import importlib                                       # noqa: E402

try:
    _is_state = importlib.import_module(
        "cognition.extract")._is_state_word
except Exception:                                       # pragma: no cover
    STATE_WORDS = ("过敏", "喜欢", "讨厌", "爱", "住在", "住", "上班", "上学",
                   "读书", "工作", "吃", "喝", "怕", "养", "想", "学", "叫")

    def _is_state(word: str) -> bool:
        return any(w in str(word or "") for w in STATE_WORDS)

CASES = [
    ("林小雨对花生过敏", "花生这个得记住。"),
    ("我妹妹对海鲜过敏", "海鲜不行，记下了。"),
    ("我妹妹不吃香菜", "香菜我记着。"),
    ("我喜欢喝美式", "美式，好。"),
    ("我妹妹喜欢喝美式", "她喜欢美式啊。"),
    ("我住在南京", "南京，记下了。"),
    ("我妹妹在广州上学", "广州挺好的。"),
    ("我妹妹怕狗", "怕狗啊。"),
]

_PAIR = re.compile(r"(.{2,12})（(.{1,8})）")


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    tag = os.environ.get("EXTRACT_MODEL_TAG", "")
    print(f"状态句抽取诊断　模型：{tag or url}")
    print("=" * 94)

    import tempfile
    from cognition.extract import remember_turn
    from cognition.memory import Memory

    bad_n = 0
    for user, reply in CASES:
        # ⚠ **只调一次抽取**：`remember_turn` 会把它实际用的 info 返回回来。
        #   第一版先调 `extract_turn` 打印"原始抽取"、再调 `remember_turn`，
        #   那是**两次独立的模型调用**，结果当然不一样——
        #   于是"原始抽取"和"最终入库"两行根本不可比（实测踩过）。
        d = pathlib.Path(tempfile.mkdtemp())
        mem = Memory(str(d / "m.db"))
        try:
            info = remember_turn(mem, gen, user, reply, session="s", turn=1,
                                 index=None) or {}
        except Exception as exc:
            print(f"\n用户：{user}\n  （入库失败：{type(exc).__name__}: {exc}）")
            continue
        facts = [str(x) for x in (info.get("facts") or [])]
        rels = [str(x) for x in (info.get("relations") or [])]
        print(f"\n用户：{user}")
        print(f"  抽取 facts     = {facts}")
        print(f"  抽取 relations = {rels}")
        bad = []
        for r in rels:
            for m in _PAIR.finditer(r):
                if _is_state(m.group(2)):
                    bad.append(f"{m.group(1)}（{m.group(2)}）")
        if bad:
            bad_n += 1
            print(f"  ⚠ **状态词被当成关系词**：{bad}")

        stored = [f"[{i['kind']}] {i['content']}"
                  for i in mem.all_semantic(status="active")]
        print(f"  **最终入库** = {stored}")
        obj = [w for w in ("花生", "海鲜", "香菜", "美式", "南京", "广州", "狗")
               if w in user]
        kept = [w for w in obj if any(w in s for s in stored)]
        if obj:
            print(f"  原话里的宾语 {obj} → 入库保住了 {kept}"
                  + ("　✅" if kept == obj else "　✗ **丢了**"))
    print()
    print("=" * 94)
    print(f"原始抽取里出现「状态词当关系」的：{bad_n}/{len(CASES)}")
    print("（修复在 `remember_turn`：把状态句从原话还原成事实，看「最终入库」那行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

