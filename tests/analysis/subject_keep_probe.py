"""第三方主语保真度：抽取会不会把「谁的事」丢掉。

## 为什么先测这个

三句互补实测里，第 3 句「小雨是广州大学的学生」被抽成 `facts=['广州大学']`
——**主语（小雨）和谓语（是学生）全丢**，于是问「小雨是哪个学校的」答不出。

这条**在别名归并的上游**：就算系统知道「小雨=林小雨」，只要事实根本没被抽出来，
归并也救不了。所以先量清楚：换掉的抽取器（Qwen3.5-2B）会不会也丢？

## 判据

对每句，看抽出的 facts/relations 里**有没有那个第三方主体**（小雨/妹妹/…）。
丢了主体 = 这条记忆无法归属到任何人，多跳必然断链。

用法：
    python tests/model_server.py run --model Qwen3.5-2B --ngl 99 \
        --script tests/analysis/subject_keep_probe.py
    python tests/model_server.py run --model v1.3 --ngl 15 \
        --script tests/analysis/subject_keep_probe.py
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

# (用户原话, 未央回答, 必须保留的主体词)
CASES = [
    ("小雨是广州大学的学生", "嗯，广州大学啊。", ["小雨"]),
    ("我妹妹在广州上学", "广州挺好的。", ["妹妹"]),
    ("我妹妹叫林小雨", "林小雨，记住了。", ["妹妹", "林小雨"]),
    ("我同事陈思远做后端", "陈思远，记下了。", ["陈思远", "同事"]),
    ("团子三岁了", "三岁啦。", ["团子"]),
    ("我妈在杭州住院了", "阿姨保重。", ["妈"]),
]


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    tag = os.environ.get("EXTRACT_MODEL_TAG", "")
    print(f"主体保真度　模型：{tag or url}")
    print("=" * 88)
    kept = 0
    for user, reply, subjects in CASES:
        info = EX.extract_turn(gen, user, reply)
        blob = " ".join([str(x) for x in
                         (info.get("facts") or []) + (info.get("relations") or [])
                         + (info.get("preferences") or [])])
        miss = [s for s in subjects if s not in blob]
        ok = not miss
        kept += ok
        print(f"{'✅' if ok else '✗'} 用户：{user}")
        print(f"     facts     = {[str(f)[:30] for f in (info.get('facts') or [])]}")
        print(f"     relations = {[str(r)[:30] for r in (info.get('relations') or [])]}")
        if miss:
            print(f"     ⚠ **丢了主体** {miss}"
                  f"——这条记忆无法归属到任何人，多跳在这里断链")
    print("=" * 88)
    print(f"主体保住：{kept}/{len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
