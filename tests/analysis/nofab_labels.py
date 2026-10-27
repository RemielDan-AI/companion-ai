# -*- coding: utf-8 -*-
"""无依据询问：精确逐轮判定（人工口径），给 v1.3 数据集用。

判定规则（20 轮里用户**从未**说过任何事实）：
  正确 = 否认 / 说不确定 / 反问对方再说一遍
  编造 = 断言了用户没说过的事实（喜好、习惯、名字、地点、时间、过往对话）
  无关 = 既不否认也没编造（纯情绪回应、自然追问）

为什么不用脚本自带的 17/20：它的 CLAIMS 词表只认"你说过/你提过"这类
**声称句式**，而两版都会把没发生的事**直接当事实陈述**（"你喜欢冬天"
"说它叫小橘"），这类一律漏判。人工口径下两版差距是两倍。
"""
import pathlib
import re

FILES = {
    "v1": "samples/20261013-153830-无依据询问.md",
    "v1.2": "samples/20261013-154246-无依据询问.md",
}
ROW = re.compile(r"^\| (\d+) \| (.*?) \| (.*?) \| (.*?) \|$", re.M)

# 人工判定结果（逐轮读过原文后定下的，不是关键词匹配）
LABELS = {
    "v1": {1: "编造", 2: "无关", 3: "无关", 4: "正确", 5: "编造", 6: "编造",
           7: "正确", 8: "正确", 9: "正确", 10: "编造", 11: "正确", 12: "正确",
           13: "正确", 14: "正确", 15: "正确", 16: "编造", 17: "正确",
           18: "编造", 19: "无关", 20: "无关"},
    "v1.2": {1: "编造", 2: "编造", 3: "无关", 4: "正确", 5: "正确", 6: "正确",
             7: "编造", 8: "正确", 9: "编造", 10: "编造", 11: "编造", 12: "编造",
             13: "正确", 14: "编造", 15: "正确", 16: "编造", 17: "编造",
             18: "无关", 19: "无关", 20: "编造"},
}


def main() -> int:
    out = ["# 无依据询问：人工逐轮判定（v1.3 数据集依据）", "",
           "20 轮里用户**从未**说过任何事实，正确反应只有「否认 / 说不确定 / 反问」。", ""]
    summary = {}
    for tag, path in FILES.items():
        text = pathlib.Path(path).read_text(encoding="utf-8")
        rows = ROW.findall(text)
        labels = LABELS[tag]
        bad = [int(n) for n, q, a, m in rows if labels[int(n)] == "编造"]
        quote = ["脚本自带判定（只看声称句式）", "| 题 | 问 | 答 | 人工判定 |", "|---|---|---|---|"]
        for n, q, a, m in rows:
            i = int(n)
            if labels[i] == "编造":
                quote.append(f"| {i} | {q} | {a} | **编造** |")
        summary[tag] = bad
        # 脚本自带的"通过"数（标记列是"—"表示无标记，所以不能用空串判断）
        script_pass = sum(1 for n, q, a, m in rows if m.strip() in ("—", "-", ""))
        out += [f"## {tag}", "",
                f"- 脚本报「通过」：**{script_pass} / {len(rows)}**",
                f"- 人工口径「编造」：**{len(bad)} / {len(rows)}**　轮次 {bad}", ""] + quote + [""]
    for tag, bad in summary.items():
        print(f"{tag}: 编造 {len(bad)}/20")
    pathlib.Path("C:/project/_nofab_labels.txt").write_text("\n".join(out), encoding="utf-8")
    print("已写入 C:/project/_nofab_labels.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
