"""列出最近一份报告里未达标的题，并标出它属于哪个类别、为什么不达标。"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests.conv_6_selfrestraint import TURNS  # noqa: E402

CATEGORY = {str(t.get("user")) if isinstance(t, dict) else str(t): 
            (t.get("category") if isinstance(t, dict) else "（无标签）")
            for t in TURNS}

SAMPLES = ROOT / "samples"
TURN_RE = re.compile(r"\*\*(\d+)\. 用户\*\*：(.*?)\n\n> (.*?)\n\n<sub>(.*?)\s*(\*\*！[^*]*\*\*)</sub>",
                     re.S)

files = [p for p in SAMPLES.glob("*对话能力-极端情况*.md") if p.stat().st_size > 5000]
files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
text = files[0].read_text(encoding="utf-8", errors="replace")
print(f"报告：{files[0].name}\n")
for number, user, reply, meta, problem in TURN_RE.findall(text):
    user = user.strip()
    category = CATEGORY.get(user, "（未匹配到标签）")
    print(f"#{number}【{category}】{user[:30]}")
    print(f"    问题：{problem.strip('*！ ')}")
    print(f"    回复：{reply.strip()[:70]}")
