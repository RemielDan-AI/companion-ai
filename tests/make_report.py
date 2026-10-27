#!/usr/bin/env python
"""把测试结果做成能看的报告：并排比对 + 总目录（HTML）。

用法：
    python tests/make_report.py --compare            # 自动配对最近两份「极端情况」报告并排比对
    python tests/make_report.py --compare A.md B.md  # 指定两份比对
    python tests/make_report.py --index              # 给 samples/ 下所有报告做一张总目录
    python tests/make_report.py --compare --open     # 生成后直接打开

生成的文件都放在 samples/ 下，直接双击用浏览器看。
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys
import webbrowser

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

TURN_RE = re.compile(
    r"\*\*(\d+)\. 用户\*\*：(.*?)\n\n> (.*?)\n\n<sub>(.*?)</sub>", re.S)
CAT_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
                        r"\s*(\d+)\s*\|\s*(\d+)\s*\|\s*$", re.M)
METRIC_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$", re.M)

CSS = """
body{font-family:'Microsoft YaHei',system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1b1b1b}
header{background:#1f2937;color:#fff;padding:14px 20px}
header h1{margin:0;font-size:18px}
header .sub{opacity:.8;font-size:12px;margin-top:4px}
.wrap{padding:16px 20px 60px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #e5e7eb;padding:8px 10px;vertical-align:top;text-align:left}
th{background:#f1f5f9;position:sticky;top:0;z-index:1;font-weight:600}
tr.bad td{background:#fff5f5}
tr.warn td{background:#fffbeb}
td.user{width:23%;color:#111}
td.reply{width:31%;white-space:pre-wrap;line-height:1.5}
.meta{color:#6b7280;font-size:11px;margin-top:4px}
.tag{display:inline-block;padding:1px 6px;border-radius:999px;font-size:11px;margin-right:4px}
.tag.ok{background:#dcfce7;color:#166534}
.tag.bad{background:#fee2e2;color:#991b1b}
.tag.info{background:#e0e7ff;color:#3730a3}
h2{font-size:15px;margin:22px 0 8px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px 14px;min-width:130px}
.card .k{color:#6b7280;font-size:11px}
.card .v{font-size:18px;font-weight:600;margin-top:2px}
a{color:#1a5fb4}
"""


def parse_report(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    title = (text.splitlines() or [""])[0].lstrip("# ").strip()
    relaxed = "放宽约束" in title or "跳过本地拒绝规则" in text
    model = ""
    match = re.search(r"^-\s*模型：(.*)$", text, re.M)
    if match:
        model = match.group(1).strip()
    if not model:      # 老报告没有这一行，就去正文里找模型文件名的痕迹
        found = re.findall(r"([A-Za-z0-9._-]+\.gguf)", text)
        model = found[0] if found else "未记录"
    rows = []
    for number, user, reply, meta in TURN_RE.findall(text):
        bad = "！" in meta
        rows.append({"index": int(number), "user": user.strip(), "reply": reply.strip(),
                     "meta": meta.strip(), "ok": not bad,
                     "problems": meta.split("！")[-1].strip() if bad else ""})
    metrics = {}
    for key, value in METRIC_RE.findall(text):
        key, value = key.strip(), value.strip()
        if key in ("指标", "类别", "轮数") or value == "值" or set(value) <= set("-"):
            continue
        metrics.setdefault(key, value)
    categories = [{"name": m[0], "n": int(m[1]), "ok": int(m[2]), "refused": int(m[3]),
                   "self": int(m[4]), "risky": int(m[5])}
                  for m in CAT_ROW_RE.findall(text)]
    return {"path": path, "title": title, "relaxed": relaxed, "rows": rows,
            "model": model,
            "metrics": metrics, "categories": categories,
            "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime)}


def newest(pattern: str) -> pathlib.Path | None:
    files = sorted(SAMPLES.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def need(name: str) -> str:
    """把路径缩成 samples 下的名字，方便页面上看。"""
    return pathlib.Path(name).name


def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_compare(a: dict, b: dict) -> pathlib.Path:
    a_rows = {row["index"]: row for row in a["rows"]}
    b_rows = {row["index"]: row for row in b["rows"]}
    indexes = sorted(set(a_rows) | set(b_rows))
    diffs = sum(1 for i in indexes
                if a_rows.get(i, {}).get("ok") != b_rows.get(i, {}).get("ok"))
    only_b_bad = sum(1 for i in indexes
                     if a_rows.get(i, {}).get("ok") and not b_rows.get(i, {}).get("ok"))
    html = [f"<!doctype html><meta charset='utf-8'><title>比对：{esc(a['title'])} vs "
            f"{esc(b['title'])}</title><style>{CSS}</style>",
            "<header><h1>对话测试并排比对</h1>",
            f"<div class='sub'>模型：<b>{esc(a.get('model', '未记录'))}</b>"
            + (f"（右侧同模型）" if a.get("model") == b.get("model")
               else f"　|　右侧模型：<b>{esc(b.get('model', '未记录'))}</b>")
            + "<br>"
            f"左：{esc(a['title'])}（{need(a['path'])}）　"
            f"右：{esc(b['title'])}（{need(b['path'])}）　"
            f"生成于 {dt.datetime.now():%Y-%m-%d %H:%M}</div></header>", "<div class='wrap'>",
            "<div class='cards'>",
            f"<div class='card'><div class='k'>题目数</div><div class='v'>{len(indexes)}</div></div>",
            f"<div class='card'><div class='k'>两侧结论不同的题</div><div class='v'>{diffs}</div></div>",
            f"<div class='card'><div class='k'>左边过、右边没过</div><div class='v'>{only_b_bad}</div></div>",
            "</div>",
            "<p class='meta'>红色＝这一侧没达标（未拒绝 / 缺内容 / 太长 / 出错）；"
            "本页只做并排展示，判断标准见各自报告的总览与分类统计。</p>",
            "<h2>分类对照</h2><table><tr><th>类别</th>"
            f"<th>{esc(a['title'])}：达标/系统拒绝/模型自拒/疑似给做法</th>"
            f"<th>{esc(b['title'])}：达标/系统拒绝/模型自拒/疑似给做法</th></tr>"]
    b_cats = {c["name"]: c for c in b["categories"]}
    for cat in a["categories"] or b["categories"]:
        name = cat["name"]
        left = f"{cat['ok']}/{cat['n']}　{cat['refused']}　{cat['self']}　{cat['risky']}"
        right_cat = b_cats.get(name)
        right = (f"{right_cat['ok']}/{right_cat['n']}　{right_cat['refused']}　"
                 f"{right_cat['self']}　{right_cat['risky']}") if right_cat else "—"
        flag = " warn" if right_cat and right_cat["ok"] < right_cat["n"] else ""
        html.append(f"<tr class='{flag.strip()}'><td>{esc(name)}</td><td>{left}</td>"
                    f"<td>{right}</td></tr>")
    html += ["</table>", "<h2>逐题并排</h2><table>",
             "<tr><th>#</th><th>用户</th><th>左：回复</th><th>右：回复</th></tr>"]
    for index in indexes:
        left, right = a_rows.get(index), b_rows.get(index)
        cls = ""
        if left and right and left["ok"] != right["ok"]:
            cls = " bad"
        html.append("<tr class='%s'><td>%d</td><td class='user'>%s</td>"
                    % (cls.strip(), index, esc(left["user"] if left else
                                               (right["user"] if right else ""))))
        for row in (left, right):
            if not row:
                html.append("<td class='reply'>—</td>")
                continue
            tag = ("<span class='tag ok'>达标</span>" if row["ok"]
                   else "<span class='tag bad'>未达标</span>")
            html.append(f"<td class='reply'>{tag}{esc(row['reply'])}"
                        f"<div class='meta'>{esc(row['meta'])}</div></td>")
        html.append("</tr>")
    html += ["</table></div>"]
    out = SAMPLES / f"比对-{a['path'].stem[:20]}-vs-{b['path'].stem[:20]}.html"
    out.write_text("\n".join(html), encoding="utf-8")
    return out


def build_index() -> pathlib.Path:
    reports = []
    for path in sorted(SAMPLES.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        if "对话能力" not in path.name and "模型对照" not in path.name \
                and "全量回归" not in path.name:
            continue
        try:
            reports.append(parse_report(path))
        except Exception:
            continue
    html = [f"<!doctype html><meta charset='utf-8'><title>测试报告总目录</title>"
            f"<style>{CSS}</style>",
            "<header><h1>测试报告总目录</h1><div class='sub'>"
            f"samples/ 下共 {len(reports)} 份　生成于 {dt.datetime.now():%Y-%m-%d %H:%M}"
            "</div></header><div class='wrap'><table>",
            "<tr><th>报告</th><th>模型</th><th>模式</th><th>题目数</th><th>达标</th>"
            "<th>未达标</th><th>分类统计里的弱项</th><th>时间</th></tr>"]
    for item in reports:
        weak = [f"{c['name']} {c['ok']}/{c['n']}"
                for c in item["categories"] if c["ok"] < c["n"]]
        total = len(item["rows"])
        ok = sum(1 for row in item["rows"] if row["ok"])
        mode = "放宽约束" if item["relaxed"] else "正常"
        html.append(
            f"<tr class='{'' if ok == total else 'bad'}'><td>{esc(item['title'])}<div class='meta'>"
            f"{esc(item['path'].name)}</div></td>"
            f"<td>{esc(item.get('model', '未记录'))}</td><td>{mode}</td><td>{total}</td>"
            f"<td>{ok}</td><td>{total - ok}</td>"
            f"<td>{esc('；'.join(weak) or '—')}</td>"
            f"<td>{item['mtime']:%m-%d %H:%M}</td></tr>")
    html += ["</table></div>"]
    out = SAMPLES / "报告总目录.html"
    out.write_text("\n".join(html), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="把测试报告做成能看的 HTML")
    parser.add_argument("--compare", nargs="*", default=None,
                        help="两份报告路径；不给就自动配对最近的两份极端情况报告")
    parser.add_argument("--index", action="store_true", help="生成报告总目录")
    parser.add_argument("--open", action="store_true", help="生成后打开")
    args = parser.parse_args()

    outs = []
    if args.index:
        outs.append(build_index())
    if args.compare is not None:
        if len(args.compare) >= 2:
            paths = [pathlib.Path(p) for p in args.compare[:2]]
        else:
            normal = newest("*对话能力-极端情况与自我约束.md")
            relaxed = newest("*对话能力-放宽约束下的自我约束.md")
            paths = [p for p in (normal, relaxed) if p]
        if len(paths) < 2:
            print("没找到两份可配对的报告，请显式给两个路径")
        else:
            a, b = (parse_report(p) for p in paths)
            outs.append(build_compare(a, b))
    if not outs:
        outs.append(build_index())

    for path in outs:
        print("已生成：", path)
        if args.open:
            webbrowser.open(path.as_uri())
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
