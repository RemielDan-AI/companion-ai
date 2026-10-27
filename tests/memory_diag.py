"""B 诊断：记忆类失败到底是"检索没给对"还是"给了它不说"。

做法：真跑一遍记忆场景（先喂埋点，再逐个回问），但把每轮**实际注入给模型的记忆块**
记下来，然后逐题核对：
    · 期望的事实出现在注入块里 → 检索没问题，是生成端不说（要靠微调/反例）
    · 期望的事实不在注入块里   → 检索没给对（要先修检索，光靠微调治不了根）

用法：python tests/memory_diag.py [方向] [变体]
"""
from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "dialogues"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import Companion  # noqa: E402
import framework  # noqa: E402

# 回问题时，期望事实里至少有一个词出现在注入块里
PROBES = {
    "你还记得我叫什么吗": ["林一"],
    "我家猫叫什么来着": ["团子", "煤球"],
    "我住在哪个城市": ["杭州", "成都"],
    "我妹妹叫什么": ["林小雨", "小雨"],
    "我对什么过敏来着": ["芒果"],
    "我喝咖啡怎么喝": ["美式"],
    "我最喜欢哪个季节": ["秋"],
    "我上班坐哪条线": ["地铁", "二号线", "2号线"],
    "我晚上一般几点睡": ["十二点", "十二"],
    "我在学什么乐器": ["吉他"],
    "我睡前有什么习惯": ["看书", "书"],
    "我讨厌什么样的会": ["议程"],
}


class Probe(Companion):
    """记下每轮实际注入给模型的记忆块。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.injected: dict[str, str] = {}

    def _select_memory_block(self, user_text, decision):
        block = super()._select_memory_block(user_text, decision)
        self.injected[user_text] = block or ""
        return block


def main() -> int:
    direction = sys.argv[1] if len(sys.argv) > 1 else "记忆连贯"
    variant = sys.argv[2] if len(sys.argv) > 2 else "搬家换城市"
    tmp = pathlib.Path(__file__).resolve().parent / "_tmp" / "memdiag"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    companion = Probe(memory_path=tmp / "db", index_path=tmp / "i.json",
                      profile_path=tmp / "p.json", values_path=tmp / "v.yaml",
                      scenarios_path=tmp / "s.jsonl",
                      preferences_path=tmp / "pr.jsonl",
                      ai_profile_path=tmp / "a.yaml")

    turns = framework.build(direction, variant)
    seed = [t for t in turns[:20] if isinstance(t, str)]
    print(f"先喂 {len(seed)} 轮埋点…")
    for index, text in enumerate(seed, 1):
        try:
            companion.say(text)
        except Exception as exc:
            print(f"  {index}. 出错：{exc}")
        if index % 5 == 0:
            print(f"  …{index} 轮")

    print("\n=== 逐题核对（注入块里有没有那条事实）===")
    missed_retrieval, missed_generation, failed = [], [], []
    for question, expects in PROBES.items():
        try:
            reply, _info = companion.say(question)
        except Exception as exc:
            reply = f"<出错：{exc}>"
        if str(reply).startswith("<出错"):
            # 生成失败绝不能算"正常"——服务没起时必须一眼看出来
            failed.append(question)
            print(f"\n问：{question}　→ 生成失败（服务没起或连不上）：{str(reply)[:60]}")
            continue
        block = companion.injected.get(question, "")
        in_block = any(word in block for word in expects)
        said = any(word in reply for word in expects)
        if not in_block:
            verdict = "检索没给对"
            missed_retrieval.append(question)
        elif not said:
            verdict = "给了它没说出来"
            missed_generation.append(question)
        else:
            verdict = "正常"
        print(f"\n问：{question}　期望：{'/'.join(expects)}")
        print(f"  注入块：{block.replace(chr(10), ' / ')[:110] if block else '（空）'}")
        print(f"  回答：{reply[:70]}")
        print(f"  → {verdict}")

    print(f"\n—— 生成失败：{len(failed)} 题"
          + ("（服务没起的话，下面的数字不能看）" if failed else ""))
    print(f"—— 检索没给对：{len(missed_retrieval)} 题　{missed_retrieval}")
    print(f"—— 给了没说出来：{len(missed_generation)} 题　{missed_generation}")
    companion.memory.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
