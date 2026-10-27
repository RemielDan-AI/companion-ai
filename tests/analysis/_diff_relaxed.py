# -*- coding: utf-8 -*-
"""把两份"放宽约束"报告逐题并排，标出判定不同的题。"""
import pathlib
import re

A = "samples/20261013-145902-对话能力-放宽约束下的自我约束.md"
B = "samples/20261013-151407-对话能力-放宽约束下的自我约束.md"
PAT = re.compile(r"\*\*(\d+)\. 用户\*\*：(.*?)\n\n> (.*?)\n\n<sub>(.*?)</sub>", re.S)


def parse(path):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    out = {}
    for idx, user, reply, meta in PAT.findall(text):
        out[int(idx)] = {
            "user": re.sub(r"\s+", " ", user).strip(),
            "reply": re.sub(r"\s+", " ", reply).strip(),
            "refused": "模型自己没拒绝" not in meta,
            "cat": (meta.split("·")[1].strip() if "·" in meta else "?"),
        }
    return out


a, b = parse(A), parse(B)
better = [i for i in sorted(a) if b[i]["refused"] and not a[i]["refused"]]
worse = [i for i in sorted(a) if a[i]["refused"] and not b[i]["refused"]]

lines = ["# v1 vs v1.2 逐题并排（脱离本地拒绝规则）", "",
         f"- v1 自己拒绝：{sum(1 for i in a if a[i]['refused'])}/{len(a)}",
         f"- v1.2 自己拒绝：{sum(1 for i in b if b[i]['refused'])}/{len(b)}",
         f"- v1.2 变好（v1 没守住、v1.2 守住）：{better}",
         f"- v1.2 变差（v1 守住、v1.2 没守住）：{worse}", "",
         "---", ""]
for i in sorted(set(better) | set(worse)):
    tag = "v1.2 变好" if i in better else "v1.2 变差"
    lines += [f"## 第 {i} 题　（{tag}）",
              f"- 用户：{a[i]['user'][:80]}",
              f"- v1{'　✅自己拒绝' if a[i]['refused'] else '　❌没拒绝'}：{a[i]['reply'][:170]}",
              f"- v1.2{'　✅自己拒绝' if b[i]['refused'] else '　❌没拒绝'}：{b[i]['reply'][:170]}",
              ""]

pathlib.Path("C:/project/_diff.txt").write_text("\n".join(lines), encoding="utf-8")
print("变好:", better)
print("变差:", worse)
print("已写入 C:/project/_diff.txt")
