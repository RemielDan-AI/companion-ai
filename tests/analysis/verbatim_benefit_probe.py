"""实测第③层（逐字原话注入）到底有没有用——**真实链路 A/B**。

## 要回答的问题

第③层的价值主张：**当语义库（抽取器的渲染版）里没有这条事实时，
原话还能兜住**。所以它本质是**保险**，不是常规增益。

因此测法必须**把语义那条掐掉**，否则抽取正常时两种设置没差别（那也算一个诚实结论）。

## 场景

    第 1 轮：用户说出一条关键事实（"我妹妹叫林小雨"）
    第 2~8 轮：闲聊填充（把第 1 轮挤出 6 轮短窗口）
    第 9 轮：问"我妹妹叫什么"

到第 9 轮时，`history` 只覆盖第 3~8 轮 → 第 1 轮的原话**既不在短窗口里，
也可能没被抽成可靠的语义条目** —— 正是第③层的用武之地。

## A/B 两轴

    A 轴：`COMPANION_NO_VERBATIM=1` 关掉第③层 vs 开着
    B 轴：语义条目**保留** vs **删掉**（模拟抽取没抽到 / 抽坏了）

四格都跑，结论才诚实：如果只在"删掉语义"那一格有效，那它确实是保险而非常规增益。

用法（要聊天模型 + 抽取器）：
    python tests/analysis/verbatim_benefit_probe.py
    （抽取器由本脚本自己拉起；聊天模型走 COMPANION_API_URL）
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 关键事实放在第 1 轮，之后用闲聊把它挤出短窗口
FILLER = [
    ("今天路上有点堵", "堵车确实烦人。"),
    ("午饭吃了碗面", "面挺好，下午有劲。"),
    ("下午开了个长会", "长会最耗人了。"),
    ("晚上想早点睡", "早点睡好啊。"),
    ("最近天气转凉了", "记得加件衣服。"),
    ("周末想在家躺着", "躺着也挺好。"),
    ("刚下楼买了点水果", "买什么水果了？"),
]
QUESTION = "我妹妹叫什么来着"
EXPECT = "林小雨"


def build(repeat: int, no_verbatim: bool, drop_semantic: bool):
    """跑一遍场景，返回（注入块, 回答, 语义库内容, history 条数）。

    ⚠ 两个坑（第一版都踩了）：
      ① 必须走 `c.say()` 再看 `c.last_used_block`——直接调 `_select_memory_block`
         会拿到**过期的 `self.last_retrieval`**（它由上层方法先 retrieve 再设），
         于是第③层永远没料可引，四格全 False。
      ② "删掉事实"必须**连 `episodes.summary` 一起清**——否则摘要通道会漏进来，
         看起来"两格都答对"，其实测的不是第③层。
    """
    if no_verbatim:
        os.environ["COMPANION_NO_VERBATIM"] = "1"
    else:
        os.environ.pop("COMPANION_NO_VERBATIM", None)

    import app as A
    d = pathlib.Path(tempfile.mkdtemp())
    c = A.Companion(memory_path=d / "m.db", index_path=d / "i.json",
                    profile_path=d / "p.json", values_path=d / "v.yaml",
                    scenarios_path=d / "sc.jsonl", preferences_path=d / "pf.jsonl",
                    ai_profile_path=d / "ai.json",
                    extract_api_url=os.environ.get("EXTRACT_URL"))

    c.say("我妹妹叫林小雨")
    for u, _r in FILLER:
        c.say(u)

    if drop_semantic:
        # 语义侧：删掉提到事实的条目
        ids = [int(it["id"]) for it in c.memory.all_semantic(status="active")
               if EXPECT in str(it.get("content") or "")]
        for i in ids:
            c.memory.conn.execute("DELETE FROM semantic WHERE id=?", (i,))
        # 情景侧：**清掉摘要**（摘要会进注入块，不清就漏）
        rows = [int(e["id"]) for e in c.memory.recent_episodes(limit=1000)
                if EXPECT in str(e.get("summary") or "")]
        for i in rows:
            c.memory.conn.execute(
                "UPDATE episodic SET summary='（已清）' WHERE id=?", (i,))
        c.memory.conn.commit()

    reply, _info = c.say(QUESTION)
    block = getattr(c, "last_used_block", "") or ""
    sem = [str(it.get("content")) for it in c.memory.all_semantic(status="active")]
    return block, reply, sem, len(c.generator.history)


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    os.environ["COMPANION_API_URL"] = url
    print(f"聊天模型：{url}")
    # 抽取器（与聊天分离）在本进程内拉起，脚本结束自动收掉
    try:
        from services.autostart import ensure_extract_service
        r = ensure_extract_service(verbose=False)
        print(f"抽取器：{r['action']}　{r['detail']}")
        os.environ["EXTRACT_URL"] = "http://127.0.0.1:8083/v1/chat/completions"
    except Exception as exc:
        print(f"抽取器没起来（{exc}），只用聊天模型")
    print("=" * 92)

    rows = []
    for drop in (False, True):
        for nov in (False, True):
            label = (f"语义{'删掉' if drop else '保留'} / 第③层"
                     f"{'关' if nov else '开'}")
            try:
                block, reply, sem, hlen = build(1, nov, drop)
            except Exception as exc:
                print(f"\n【{label}】✗ {type(exc).__name__}: {exc}")
                continue
            ok = EXPECT in (reply or "")
            has_v = "对方原话" in (block or "")
            rows.append((label, has_v, EXPECT in (block or ""), ok, reply, sem))
            print(f"\n【{label}】")
            print(f"  history 长度 {hlen} 条（{hlen//2} 轮）")
            print(f"  注入块含「对方原话」：{has_v}")
            print(f"  注入块含「{EXPECT}」：{EXPECT in (block or '')}")
            print(f"  语义库：{sem}")
            print(f"  问「{QUESTION}」→ {(reply or '')[:70]}")
            print(f"  答对（含「{EXPECT}」）：{'✅' if ok else '✗'}")

    print()
    print("=" * 92)
    print(f"{'配置':<22}{'块含原话':>9}{'答对':>6}")
    print("-" * 92)
    for label, has_v, has_name, ok, _r, _s in rows:
        print(f"{label:<22}{('有' if has_v else '无'):>9}{('✅' if ok else '✗'):>6}")
    print("-" * 92)

    def find(drop, nov):
        for label, has_v, has_name, ok, _r, _s in rows:
            if label == (f"语义{'删掉' if drop else '保留'} / 第③层"
                         f"{'关' if nov else '开'}"):
                return ok
        return None

    print()
    keep_on, keep_off = find(False, False), find(False, True)
    drop_on, drop_off = find(True, False), find(True, True)
    print(f"语义保留：第③层开={keep_on}　关={keep_off}")
    print(f"语义删掉：第③层开={drop_on}　关={drop_off}")
    print()
    if drop_on and not drop_off:
        print("→ **第③层确实是保险**：只在语义缺失时救回来（抽取正常时无差别）")
    elif drop_on and drop_off:
        print("→ 语义删掉后两格都对：可能短窗口或聊天模型本身还记得（测试场景不够长）")
    elif not drop_on and not drop_off:
        print("→ 语义删掉后两格都答不出：第③层没能救回来（要查注入或预算）")
    else:
        print("→ 结果不典型，看上面的逐格输出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
