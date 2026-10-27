"""钉住「第③层：逐字原话注入」。

## 背景

`retrieve()` 一直返回 `recent`（`episodic.user_text` 的**逐字原话**），
但**全仓没人消费 → 从不进提示**。而语义库是**抽取器的渲染版**、会失真
（实测「林小雨」被抽成「林小记得」），情景摘要也只是 summary。
所以 **6 轮之外的原话此前完全没进过提示**，尽管它一直存在库里。

## 这里最容易错的一点（我第一版就错了）

`recent_episodes` 是 **`ORDER BY id DESC`（最新在前）**，而短窗口
（`Generator.history`，`history_turns` 轮原话）**已经吃掉了最新的那几轮**。
默认 `recent_limit=3` 比历史窗口（6）还小 → **排除后永远是空**，
整层等于死代码。所以调用方必须按 history 的实际长度**自适应取够**。

本测试**不连模型、不起服务**。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.retrieve import (VERBATIM_ITEM_CHARS, VERBATIM_MAX_ITEMS,  # noqa: E402
                             _verbatim_lines, build_memory_block)

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def result_with(recent_texts, semantic=None, episodes=None) -> dict:
    return {
        "recent": [{"id": i, "user_text": t, "reply_text": "答"}
                   for i, t in enumerate(recent_texts, 1)],
        "semantic": semantic or [{"id": 1, "kind": "fact", "content": "对方对芒果过敏"}],
        "episodes": episodes or [],
    }


def main() -> int:
    print("=" * 78)
    print("① 只挑 short-term 窗口**之外**的原话（窗口内的是纯重复）")
    print("=" * 78)
    # recent 最新在前，history 覆盖最新 3 条
    res = result_with(["第5句原话", "第4句原话", "第3句原话", "第2句原话", "第1句原话"])
    hist = {"第5句原话", "第4句原话", "第3句原话"}
    got = _verbatim_lines(res, hist)
    check(len(got) > 0, "窗口外还有原话可取", f"{got}")
    check(all("第5句" not in g and "第4句" not in g and "第3句" not in g
              for g in got), "没引窗口内的", f"{got}")
    check(any("第2句" in g for g in got), "引了窗口外的", f"{got}")

    print()
    print("=" * 78)
    print("② 上限：条数、每条长度、太短的跳过")
    print("=" * 78)
    long_res = result_with(["这是一句很长很长很长的原话" * 5, "短句够格", "第三句在这里"])
    got2 = _verbatim_lines(long_res, set())
    check(len(got2) <= VERBATIM_MAX_ITEMS, f"最多 {VERBATIM_MAX_ITEMS} 条",
          f"{len(got2)} 条")
    if got2:
        inner = got2[0].strip("“”")
        check(len(inner) <= VERBATIM_ITEM_CHARS, f"每条不超过 {VERBATIM_ITEM_CHARS} 字",
              f"{len(inner)} 字")
    check(_verbatim_lines(result_with(["短", "也短"]), set()) == [],
          "太短（<4 字）的不引")

    print()
    print("=" * 78)
    print("③ 注入块里出现「- 对方原话：」；没有可引时**不出现**该行")
    print("=" * 78)
    blk = build_memory_block(result_with(["我公司要搬去滨江", "我养了只猫叫团子"]))
    check("- 对方原话：" in blk, "有可引的原话时出现该行")
    check("我公司要搬去滨江" in blk, "原话内容进了块")
    blk2 = build_memory_block(result_with(["同一句", "同一句"]),
                              exclude_texts={"同一句"})
    check("- 对方原话：" not in blk2, "全被排除时不出现该行（不留空标签）")

    print()
    print("=" * 78)
    print("④ 预算：原话有**保底**，不能被截断砍掉")
    print("=" * 78)
    # 塞很多事实把预算占满，看原话是否还在
    many = [{"id": i, "kind": "fact", "content": f"对方的事实条目第{i}号内容"}
            for i in range(1, 15)]
    res_big = result_with(["这句原话必须活下来"], semantic=many)
    blk3 = build_memory_block(res_big, max_chars=380)
    check("这句原话必须活下来" in blk3, "预算被事实占满时原话仍在",
          f"块长 {len(blk3)}")
    check(len(blk3) <= 380, "块长不超上限", f"{len(blk3)}")

    print()
    print("=" * 78)
    print("⑤ 接线：调用方必须传 exclude_texts 且 recent_limit 自适应（防退回死代码）")
    print("=" * 78)
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    check("exclude_texts=" in src, "build_memory_block 传了 exclude_texts")
    check("recent_limit=" in src,
          "retrieve 传了显式 recent_limit（默认 3 比历史窗口还小 → 会永远是空）")
    check("len(self.generator.history)" in src, "recent_limit 按 history 长度自适应")
    rd = (ROOT / "engine" / "retrieve.py").read_text(encoding="utf-8")
    check("_verbatim_lines" in rd, "retrieve 侧有 _verbatim_lines")

    print()
    print("=" * 78)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
