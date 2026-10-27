"""钉住「名字识别」的统一口径（`cognition/terms.py`）。

## 为什么要它

2026-10-15 梳理发现，"名字"在四处各写了一套正则、标准还不同：

    engine/retrieve.py: known_names()          取 2–4 字
    cognition/extract.py: _enrich_relation()   叫(名字{2,4})
    cognition/memory.py: link_by_entity()      叫(名字{2,3})
    engine/retrieve.py: _HOP_RE                （曾写死 {2,4}）

后果**不是"抓不到"而是"抓出错的前缀"**（实测）：

    '公司叫工业软件公司'     ← 正确名字是「工业软件公司」
      {2,3} → ['工业软']      ← **假实体**，还被拿去建链
      {2,4} → ['工业软件']    ← **假实体**

本测试钉住：① 长名字抓全 ② 无标点时不过抓 ③ 机构名里的常见字
（有/学/上/读）不被误裁 ④ 三个调用点确实都走同一口径（静态检查）。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import terms as T                       # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


CASES = [
    # --- 长度：原来被截断的 ---
    ("公司叫工业软件公司", ["工业软件公司"], "6 字机构名（原来抓成「工业软」「工业软件」）"),
    ("公司叫北京字节跳动科技有限公司", ["北京字节跳动科技有限公司"],
     "11 字机构名（含「有」，不能被裁）"),
    ("我在一家叫南京大学的研究所上班", ["南京大学"],
     "机构名里的「学」不能被裁；「的」是边界要收尾"),
    # --- 边界：无标点也不能吞谓语 ---
    ("我妹妹叫林小雨她在读大学", ["林小雨"], "无标点：靠「她」收尾"),
    ("我女儿叫王小明在上海读书", ["王小明"], "无标点：靠「在」收尾"),
    ("我妹妹叫林小雨，她在读大学", ["林小雨"], "有标点：直接断在标点"),
    # --- 基本 ---
    ("我妹妹叫林小雨", ["林小雨"], "基本"),
    ("我养了只猫叫团子", ["团子"], "宠物"),
    ("我妹妹叫林小雨，妹妹叫林小雨", ["林小雨"], "同一名字去重"),
    ("我在一家公司上班", [], "没有「叫」→ 空"),
    # --- 前两字保护：名字里含裁剪字也不能被裁 ---
    ("他叫在熙", ["在熙"], "名字首字是裁剪字（前两字永不裁）"),
]

SPECIAL: dict = {}


def main() -> int:
    print("=" * 78)
    print("① find_called_names：长名字抓全、无标点不过抓、机构常用字不误裁")
    print("=" * 78)
    for text, want, why in CASES:
        got = T.find_called_names(text)
        if text in SPECIAL:
            ok = SPECIAL[text](got)
        else:
            ok = got == want
        check(ok, why, f"{text!r} → {got}"
              + ("" if ok else f"　期望 {want}"))

    print()
    print("=" * 78)
    print("② 长度界限是共享常量（三处不再各写一套）")
    print("=" * 78)
    check(T.NAME_MIN == 2, f"NAME_MIN={T.NAME_MIN}")
    check(T.NAME_MAX >= 8, f"NAME_MAX={T.NAME_MAX}（机构名要放得下）")
    check(T.trim_name("林小雨在读大学") == "林小雨",
          "trim_name 能裁掉谓语", repr(T.trim_name("林小雨在读大学")))
    check(T.trim_name("北京字节跳动科技有限公司") == "北京字节跳动科技有限公司",
          "trim_name 不裁机构名里的「有」")
    check(T.trim_name("在熙") == "在熙", "trim_name 前两字永不裁")

    print()
    print("=" * 78)
    print("③ 「名字（关系）」正则也共用（extract 产出 / retrieve 解析必须一致）")
    print("=" * 78)
    m = T.PAIR_RE.search("工业软件公司（上班的地方）")
    check(bool(m) and m.group(1) == "工业软件公司",
          "长名字不被截断", f"{m.groups() if m else None}")
    m2 = T.PAIR_RE.search("林小雨（妹妹）")
    check(bool(m2) and m2.groups() == ("林小雨", "妹妹"), "基本形态", f"{m2.groups()}")
    check(T.is_name_like("工业软件公司"), "is_name_like 认 6 字机构名")
    check(not T.is_name_like("a"), "is_name_like 拒绝太短的")

    print()
    print("=" * 78)
    print("③b is_name_like 必须把**整句**挡在外面（否则 keywords 的挖词会毁掉二元组）")
    print("=" * 78)
    # 实测：`known_names()` 曾收进「林小雨不能吃花生」这种整句，
    # 而 keywords() 的"整词切出并挖掉"是为短专名设计的 →
    # 整句被挖成空白 → 二元组全毁 → 一跳的第二次检索拿到 0 分、彻底失效。
    for text, want, why in (
        ("林小雨", True, "真名字"),
        ("团子", True, "宠物名"),
        ("妹妹", True, "关系词"),
        ("北京字节跳动科技有限公司", True, "长机构名（含「有」，不该被拒）"),
        ("林小雨不能吃花生", False, "**整句事实**：含虚词「不」"),
        ("用户妹妹林小雨在读大学", False, "整句事实：含「在」"),
        ("用户对芒果过敏", False, "整句事实：以主体开头 + 含「对」"),
        ("对方住在南京", False, "整句事实：以主体开头"),
        # ⚠ 实测漏网：`known_names()` 剥掉「对方」前缀后，把
        #    「妹妹叫林小雨」当成名字收进来，而 keywords() 会把它整词挖掉。
        #    所以「叫」必须进 `_NOT_IN_NAME`——但**不能**进 `_TRIM_AT`
        #    （trim_name 靠它抓「公司叫工业软件公司」这种长机构名）。
        ("妹妹叫林小雨", False, "带「叫」的短句不是名字"),
    ):
        check(T.is_name_like(text) == want, why, f"{text!r} → {T.is_name_like(text)}")

    print()
    print("③c 「叫」不能进 _TRIM_AT（否则长机构名又被截断）")
    print("=" * 78)
    check("叫" in T._NOT_IN_NAME, "「叫」在 _NOT_IN_NAME 里")
    check("叫" not in T._TRIM_AT, "「叫」**不在** _TRIM_AT 里")
    m = T._CALLED_RE.search("公司叫工业软件公司")
    check(bool(m) and T.trim_name(m.group(1)) == "工业软件公司",
          "长机构名仍能抓全", T.trim_name(m.group(1)) if m else "没抓到")

    print()
    print("=" * 78)
    print("④ 静态检查：三个调用点确实走同一口径（防以后又各写一套）")
    print("=" * 78)
    ext = (ROOT / "cognition" / "extract.py").read_text(encoding="utf-8")
    mem = (ROOT / "cognition" / "memory.py").read_text(encoding="utf-8")
    ret = (ROOT / "engine" / "retrieve.py").read_text(encoding="utf-8")
    check("find_called_names" in ext, "extract.py 用了共享口径")
    check("find_called_names" in mem, "memory.py 用了共享口径")
    check("PAIR_RE" in ret, "retrieve.py 用了共享的配对正则")
    check("is_name_like" in ret, "retrieve.py 的 known_names 用了共享判据")
    # 旧写法不该再出现
    check('叫([\\u4e00-\\u9fff]{2,3})' not in mem, "memory.py 里没有残留的 {2,3} 旧写法")
    check('叫([\\u4e00-\\u9fff]{2,4})' not in ext, "extract.py 里没有残留的 {2,4} 旧写法")

    print()
    print("=" * 78)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
