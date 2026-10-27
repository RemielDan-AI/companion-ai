"""用新判定回头体检已有报告里的回复（不调模型）。

作用是回答："换成新判定后，上一轮报出来的'疑似给了做法'还剩几条？"
用法：python tests/risky_recheck.py [报告名关键词]
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests.conv_harness import risky_hits  # noqa: E402

SAMPLES = ROOT / "samples"
TURN_RE = None


def main() -> int:
    import re
    global TURN_RE
    TURN_RE = re.compile(r"\*\*(\d+)\. 用户\*\*：(.*?)\n\n> (.*?)\n\n<sub>(.*?)</sub>", re.S)
    keyword = sys.argv[1] if len(sys.argv) > 1 else "对话能力-极端情况"
    files = [p for p in SAMPLES.glob(f"*{keyword}*.md") if p.stat().st_size > 5000]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("没找到报告")
        return 1
    path = files[0]
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = TURN_RE.findall(text)
    flagged = []
    for number, user, reply, meta in rows:
        hits = risky_hits(reply)
        if hits:
            flagged.append((int(number), user.strip(), hits, reply.strip()))
    print(f"报告：{path.name}")
    print(f"总轮数 {len(rows)}，新判定认为「疑似给了做法」的有 {len(flagged)} 轮\n")
    for number, user, hits, reply in flagged:
        print(f"#{number} 用户：{user[:26]}")
        print(f"   命中：{hits}")
        print(f"   回复：{reply[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
