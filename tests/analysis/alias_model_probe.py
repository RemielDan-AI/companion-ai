"""用**真 2B**测「模型提议别名 + 程序裁决」。

## 为什么让模型掺一脚

规则只能抓**后缀简称**（林小雨→小雨）。换个叫法就抓不到：
阿雨、小林子、老林、「我家那位」……这类要靠**理解上下文**。

## 分工（关键）

    模型：**闭集选择**——从「已知的人」里挑一个，或答「新」。
          输出空间被程序限死，它**没机会编造一个新映射**。
    程序：**全部裁决**——`add_alias` 的护栏一条不少（长度/泛称/歧义/不成环/否掉不复活），
          而且回答还必须**正好落在已知名单上**才算数。

## 判据

    ① 该认出来的（真简称/昵称）→ 认出来
    ② **不该认的必须不认**：模型答「新」/编造的人 / 别的人 → 一条都不许写
       （这是本测试的重点：宁可漏，不可错）

用法（抽取器由本脚本自己拉起）：
    python tests/analysis/alias_model_probe.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pathlib                                        # noqa: E402
import tempfile                                       # noqa: E402

from cognition import alias as AL                      # noqa: E402
from cognition.memory import Memory                    # noqa: E402

# (已知的人, 关系, 用户这句话, 本轮抽出的关系名, 期望的别名或 None)
CASES = [
    ({"林小雨"}, "对方提到的人：林小雨（妹妹）",
     "小雨是广州大学的学生", ["对方提到的人：小雨（学生）"], "小雨"),
    ({"林小雨"}, "对方提到的人：林小雨（妹妹）",
     "阿雨最近挺好的", ["对方提到的人：阿雨（朋友）"], "阿雨"),
    ({"林小雨", "陈思远"}, "对方提到的人：林小雨（妹妹）",
     "思远昨天来我家了", ["对方提到的人：思远（同事）"], "思远"),
    # ⚠ 下面这些**一个都不许写**
    ({"林小雨"}, "对方提到的人：林小雨（妹妹）",
     "我同事叫王海涛", ["对方提到的人：王海涛（同事）"], None),
    ({"林小雨"}, "对方提到的人：林小雨（妹妹）",
     "我养了只猫叫团子", ["对方提到的人：团子（宠物）"], None),
    ({"林小雨"}, "对方提到的人：林小雨（妹妹）",
     "公司叫工业软件公司", ["对方提到的人：工业软件公司（单位）"], None),
]


def main() -> int:
    url = os.environ.get("EXTRACT_URL") or "http://127.0.0.1:8083/v1/chat/completions"
    from engine.generate import Generator
    gen = Generator(api_url=url, timeout=60)
    print(f"指认用模型：{url}")
    print("=" * 90)
    hit = fp = 0
    for known, rel, text, rels, want in CASES:
        d = pathlib.Path(tempfile.mkdtemp())
        m = Memory(str(d / "m.db"))
        for name in known:
            m.upsert_semantic(kind="relation",
                              content=f"对方提到的人：{name}（家人）", confidence=0.7)
        m.upsert_semantic(kind="relation", content=rel, confidence=0.7)
        m.add_episode(user_text=text, reply_text="嗯", summary="",
                      session="t", turn=1)
        got = AL.model_propose_aliases(m, gen, text, relations=rels)
        rows = [(r["alias"], r["canonical"]) for r in AL.all_aliases(m)]
        if want:
            ok = any(a == want for a, _c in rows)
            hit += ok
            flag = "✅" if ok else "✗"
        else:
            ok = not rows
            fp += (not ok)
            flag = "✅" if ok else "✗ **误认**"
        print(f"{flag} 已知 {'/'.join(sorted(known))}｜{text}")
        print(f"     模型提议：{[(g.get('alias'), g.get('answer'), g.get('action')) for g in got]}")
        print(f"     最终写入：{rows}　期望：{want or '（不许写）'}")
    print("=" * 90)
    want_n = sum(1 for c in CASES if c[4])
    print(f"该认出来的：{hit}/{want_n}　/**误认**（写了不该写的）：{fp} 条")
    print("读法：误认比漏认严重得多——写错的别名会把两个人的事混在一起。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
