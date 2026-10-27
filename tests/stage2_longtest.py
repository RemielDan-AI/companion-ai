#!/usr/bin/env python
"""阶段 2 长对话测试。

流程：先在对话里埋 8 条生活事实 → 34 轮日常闲聊把事实推远 → 8 个回忆问题。
对每个回忆问题分别判定三件事：
  1. 提取：事实有没有进语义记忆/画像
  2. 检索：问的时候有没有被检索到（看注入记忆块）
  3. 作答：回答里有没有答对

用法：
    python tests/stage2_longtest.py              # 默认 50 轮
    python tests/stage2_longtest.py --turns 30   # 缩短版
    python tests/stage2_longtest.py --keep       # 保留测试用记忆库
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import Companion  # noqa: E402
from cognition.extract import load_profile  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp"
TEST_DB = TMP / "memory_test.db"
TEST_INDEX = TMP / "embeddings_test.json"
TEST_PROFILE = TMP / "profile_test.json"
TEST_VALUES = TMP / "values_test.yaml"
TEST_SCENARIOS = TMP / "scenarios_test.jsonl"
TEST_PREFERENCES = TMP / "preferences_test.jsonl"
SAMPLES = ROOT / "samples"

# 每条：(类型, 档位, 文本, 判定关键词)
#   埋 = 在闲聊中自然说出的事实
#   聊 = 把事实推到上下文之外的日常闲聊
#   问 = 事后回忆提问
FACTS = [
    ("埋", "l1", "我叫周牧，在一家做医疗器械的公司做测试。", ["周牧", "医疗器械"]),
    ("埋", "l1", "我养了只橘猫叫团子，已经三岁了。", ["团子", "橘猫"]),
    ("埋", "l1", "我吃不了香菜，一闻就难受。", ["香菜"]),
    ("埋", "l1", "我一般凌晨两点才睡，早上九点起。", ["两点"]),
    ("埋", "l1", "周末我喜欢去江边钓鱼，一坐就是一下午。", ["钓鱼", "江边"]),
    ("埋", "l1", "我妹妹下个月结婚，我得准备份礼物。", ["妹妹", "结婚"]),
    ("埋", "l1", "我最近胃不太好，医生让我少吃辣。", ["辣", "胃"]),
    ("埋", "l1", "我打算十月去云南玩一趟，想去大理。", ["云南", "大理"]),
]

FILLER = [
    ("聊", "l0", "今天中午吃点啥好？", []),
    ("聊", "l1", "办公室里空调开太低了，有点冷。", []),
    ("聊", "l0", "推荐一部电影吧。", []),
    ("聊", "l1", "我同事小李昨天讲了个冷笑话，我笑了半天。", []),
    ("聊", "l0", "现在几点了？", []),
    ("聊", "l1", "地铁上人真多，挤得我脚都站麻了。", []),
    ("聊", "l0", "帮我起个昵称。", []),
    ("聊", "l1", "今天开了一下午的会，脑子有点木。", []),
    ("聊", "l0", "晚上要不要出去走走？", []),
    ("聊", "l1", "楼下的桂花开了，路过的时候特别香。", []),
    ("聊", "l0", "有什么简单的运动推荐吗？", []),
    ("聊", "l1", "我把阳台上的绿萝养死了，有点沮丧。", []),
    ("聊", "l0", "泡面里面加什么好吃？", []),
    ("聊", "l1", "刚才下雨了，我没带伞，跑回来的。", []),
    ("聊", "l0", "明天天气怎么样？", []),
    ("聊", "l1", "我妈打电话来，催我多穿点衣服。", []),
    ("聊", "l0", "推荐一首安静的歌。", []),
    ("聊", "l1", "今天喝了三杯咖啡，手有点抖。", []),
    ("聊", "l0", "楼下那家面馆好吃吗？", []),
    ("聊", "l1", "我新买的杯子摔了，有点心疼。", []),
    ("聊", "l0", "明天要不要早起？", []),
    ("聊", "l1", "最近老做梦，醒来记不清内容。", []),
    ("聊", "l0", "有什么好用的记事方法吗？", []),
    ("聊", "l1", "今天路过公园，看到好多人在遛狗。", []),
    ("聊", "l0", "冬天应该买什么外套？", []),
    ("聊", "l1", "我把书桌收拾了一遍，心情好多了。", []),
    ("聊", "l0", "怎么才能不那么容易走神？", []),
    ("聊", "l1", "刚才和同事聊了几句，他说他要辞职了。", []),
    ("聊", "l0", "晚上喝牛奶真的助眠吗？", []),
    ("聊", "l1", "我手机屏幕裂了一道缝，还没修。", []),
    ("聊", "l0", "推荐一个适合发呆的地方。", []),
    ("聊", "l1", "今天太阳挺好的，晒得人懒洋洋。", []),
    ("聊", "l0", "有什么不用动脑的娱乐吗？", []),
    ("聊", "l1", "我有点想学做菜，但怕麻烦。", []),
]

QUESTIONS = [
    ("问", "l1", "我家猫叫什么名字来着？", ["团子"]),
    ("问", "l1", "我是不是有什么忌口？", ["香菜"]),
    ("问", "l1", "我平时一般几点睡？", ["两点"]),
    ("问", "l1", "我周末一般喜欢干什么？", ["钓鱼"]),
    ("问", "l1", "我在哪家公司、做什么工作？", ["医疗器械", "测试"]),
    ("问", "l1", "我下个月有什么大事？", ["妹妹", "结婚"]),
    ("问", "l1", "医生让我注意什么？", ["辣", "胃"]),
    ("问", "l1", "我打算什么时候去哪儿玩？", ["云南", "大理"]),
]


def hit(text: str, keywords: list[str]) -> bool:
    return any(keyword in (text or "") for keyword in keywords)


def main() -> None:
    parser = argparse.ArgumentParser(description="阶段 2 长对话测试")
    parser.add_argument("--turns", type=int, default=50, help="总轮数（默认 50）")
    parser.add_argument("--keep", action="store_true", help="保留测试记忆库")
    args = parser.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    for path in (TEST_DB, pathlib.Path(str(TEST_DB) + "-wal"),
                 pathlib.Path(str(TEST_DB) + "-shm"), TEST_INDEX, TEST_PROFILE,
                 TEST_VALUES, TEST_SCENARIOS, TEST_PREFERENCES):
        if path.exists():
            path.unlink()

    companion = Companion(use_memory=True, session="longtest",
                          memory_path=TEST_DB, index_path=TEST_INDEX,
                          profile_path=TEST_PROFILE, values_path=TEST_VALUES,
                          scenarios_path=TEST_SCENARIOS,
                          preferences_path=TEST_PREFERENCES)
    # 关掉短期上下文：逼它只能靠长期记忆回答（本测试要验证的正是这个）
    companion.generator.history_limit = 0

    filler_needed = max(0, args.turns - len(FACTS) - len(QUESTIONS))
    filler = (FILLER * ((filler_needed // len(FILLER)) + 1))[:filler_needed]
    script = FACTS + filler + QUESTIONS

    started = time.time()
    answers: list[tuple[str, str, list[str], str]] = []

    for index, (kind, level, text, keywords) in enumerate(script, 1):
        reply, _info = companion.say(text)
        injected = companion.last_used_block
        print(f"[{index:02d}/{len(script)}] {kind} {text}")
        print(f"      未央：{reply[:90]}")
        if kind == "问":
            answers.append((text, reply, keywords, injected))
            print(f"      注入记忆：{injected.replace(chr(10), ' / ')[:170] or '（空）'}")
        sys.stdout.flush()

    elapsed = time.time() - started
    semantic_all = companion.memory.all_semantic()
    semantic_text = " ".join(item["content"] for item in semantic_all)
    profile = load_profile(TEST_PROFILE)
    profile_text = " ".join(
        entry["content"]
        for key in ("facts", "preferences", "relations")
        for entry in profile.get(key, [])
    )

    print("\n" + "=" * 68)
    print("回忆检验（提取=是否进记忆库，检索=问时是否命中，作答=回答是否答对）")
    print("=" * 68)
    extracted_ok = retrieved_ok = answered_ok = 0
    for question, reply, keywords, injected in answers:
        got_extract = hit(semantic_text + profile_text, keywords)
        got_retrieve = hit(injected, keywords)
        got_answer = hit(reply, keywords)
        extracted_ok += got_extract
        retrieved_ok += got_retrieve
        answered_ok += got_answer
        print(f"{question[:20]:<22}{'✓' if got_extract else '✗':<7}"
              f"{'✓' if got_retrieve else '✗':<7}{'✓' if got_answer else '✗':<7}"
              f"｜{reply[:40]}")

    total = len(answers)
    print()
    print(f"提取命中 {extracted_ok}/{total}　检索命中 {retrieved_ok}/{total}　作答答对 {answered_ok}/{total}")
    stats = companion.memory.stats()
    print(f"情景记忆 {stats['episodes']} 轮｜语义记忆 {stats['semantic']} 条"
          f"｜向量索引 {len(companion.index)} 条｜画像事实 {len(profile.get('facts', []))} 条"
          f"｜偏好 {len(profile.get('preferences', []))} 条")
    print(f"耗时 {elapsed/60:.1f} 分钟")

    SAMPLES.mkdir(exist_ok=True)
    summary_path = SAMPLES / f"{time.strftime('%Y%m%d-%H%M%S')}-阶段2-长对话测试.md"
    lines = ["# 阶段 2 长对话测试", "",
             f"- 总轮数：{len(script)}（埋点 {len(FACTS)}，闲聊 {len(filler)}，提问 {len(QUESTIONS)}）",
             f"- 耗时：{elapsed/60:.1f} 分钟",
             f"- 提取命中 {extracted_ok}/{total}，检索命中 {retrieved_ok}/{total}，作答答对 {answered_ok}/{total}",
             f"- 情景记忆 {stats['episodes']} 轮，语义记忆 {stats['semantic']} 条，向量索引 {len(companion.index)} 条",
             "", "## 回忆检验", ""]
    for question, reply, keywords, injected in answers:
        lines += [f"**问**：{question}", "",
                  f"**答**：{reply}", "",
                  f"注入记忆：{injected.replace(chr(10), ' / ')}", ""]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已保存：{summary_path}")

    if not args.keep:
        companion.memory.close()
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
