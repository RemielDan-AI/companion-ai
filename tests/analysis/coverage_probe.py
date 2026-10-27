"""验证**回读校验**这个检测器本身：**它会不会乱报**。

## 为什么先验它

检测器一旦乱报（把"本来就不该抽出来的话"报成漏抽），
后面接的"有目标的重抽"就会**不停重抽、白烧算力**，
而且覆盖度这个数字就没人信了。

所以先用金标准量三件事：
    ① **该报的报了没**：真的抽丢了关键信息时，`uncovered` 非空吗
    ② **不该报的报了没**：空轮/提问/纯寒暄——`clauses` 应该是**空**的
    ③ 覆盖度和"实际丢没丢"对不对得上（真阳性 vs 假阳性）

用法：
    python tests/model_server.py run --model Qwen3.5-2B --ngl 99 \
        --script tests/analysis/coverage_probe.py
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from cognition import verify as VF                     # noqa: E402
from engine.generate import Generator                  # noqa: E402

# (原话, 关键信息必须出现, 说明)
GOLD = [
    ("我妹妹叫林小雨", ["林小雨"], "显式命名"),
    ("林小雨对花生过敏", ["花生"], "状态句：宾语最易丢"),
    ("我妹妹对海鲜过敏", ["海鲜"], "状态句"),
    ("我妹妹不吃香菜", ["香菜"], "否定式"),
    ("我住在南京", ["南京"], "纯第一人称 + 地点"),
    ("我已经搬到成都了", ["成都"], "状态变更"),
    ("我养了只猫叫团子", ["团子"], "宠物命名"),
    ("团子三岁了", ["三岁"], "宠物属性"),
    ("我同事陈思远做后端", ["后端"], "同事 + 职业"),
    ("小花是我妹妹的同学", ["小花"], "第三方关系句"),
    ("我妹妹在广州大学上学", ["广州大学"], "地点 + 机构"),
    ("我妈在杭州住院了", ["住院"], "家人 + 地点 + 事件"),
]

# **不该被当成断言**的（报了就是乱报）
NOISE = [
    ("嗯，还行吧", "空轮"),
    ("你觉得我该不该转学", "只是提问"),
    ("今天好累啊", "纯状态（按设计不进 facts）"),
    ("好的，谢谢", "应答寒暄"),
    ("哈哈哈哈", "纯语气"),
    ("你呢", "反问"),
]

ROUNDS = max(1, int(os.environ.get("COVER_ROUNDS", "2")))


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"回读校验验证　模型：{os.environ.get('EXTRACT_MODEL_TAG') or url}")
    print("=" * 96)

    print("\n【一】噪声：这些**不该**被判成断言（报了就是乱报）")
    false_alarm = []
    for text, why in NOISE:
        clauses = [c for c in VF.split_clauses(text) if VF.looks_like_assertion(c)]
        mark = "✅" if not clauses else "✗ **乱报**"
        print(f"  {mark} {why:<16} {text!r}　判为断言：{clauses}")
        if clauses:
            false_alarm.append(f"{why}（{text}）→ {clauses}")

    print("\n【二】真话：关键信息抽丢时，uncovered 该非空")
    miss_and_caught = miss_but_silent = ok_and_quiet = ok_but_noisy = 0
    for text, keys, why in GOLD:
        for _ in range(ROUNDS):
            info = EX.extract_turn(gen, text, "嗯")
            blob = " ".join(str(x) for k in ("facts", "preferences", "relations")
                            for x in (info.get(k) or []))
            lost = [k for k in keys if k not in blob]
            cov = VF.coverage(text, info)
            flagged = bool(cov["uncovered"])
            if lost and flagged:
                miss_and_caught += 1
                tag = "✅ 抓到了"
            elif lost and not flagged:
                miss_but_silent += 1
                tag = "⚠ **漏报**（丢了但没报）"
            elif not lost and not flagged:
                ok_and_quiet += 1
                tag = "✅ 没丢也没报"
            else:
                ok_but_noisy += 1
                tag = "⚠ **假阳性**（没丢却报）"
            print(f"  {tag}　{why:<18} 丢={lost or '无'}"
                  f"　覆盖度={cov['ratio']}　uncovered={cov['uncovered']}")

    print()
    print("=" * 96)
    print("汇总")
    print("=" * 96)
    print(f"  噪声里乱报：            {len(false_alarm)}/{len(NOISE)}"
          f"　{'✅' if not false_alarm else '✗'}")
    print(f"  真丢 + 抓到了：         {miss_and_caught}")
    print(f"  真丢 + **漏报**：       {miss_but_silent}")
    print(f"  没丢 + 没报（对）：     {ok_and_quiet}")
    print(f"  没丢 + **假阳性**：     {ok_but_noisy}")
    print()
    total_flagged = miss_and_caught + ok_but_noisy
    if total_flagged:
        prec = miss_and_caught / total_flagged
        print(f"  报出来的里，真丢的占：  {prec:.0%}（精确率）")
    if (miss_and_caught + miss_but_silent):
        rec = miss_and_caught / (miss_and_caught + miss_but_silent)
        print(f"  真丢的里，报出来的占：  {rec:.0%}（召回率）")
    print()
    print("判读：")
    print("  · **噪声零乱报** 是底线——做不到就不能拿它当重抽闸门")
    print("  · 精确率低 → 会白重抽（烧算力）；召回率低 → 漏抽抓不到（白做）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
