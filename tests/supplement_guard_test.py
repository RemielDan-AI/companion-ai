"""钉住「两段式补充」的判定工具与配置默认值（不连模型）。

## 为什么要有

补充是**第二步生成**，判错了就直接影响用户看到的话。三条护栏缺一不可：

    ① **逐句**二元组判重复（整段比会被长补充稀释 —— 实测漏放过）
    ② 补充剪编造剪光就丢（**不能**接一句"我记不清"兜底，会自相矛盾）
    ③ 配置**默认关**（这是实验特性，不能悄悄上线）

⚠ 这里的不对称性和"记忆"那边**相反**：
      记忆：误杀 = 丢记忆，比漏拦更糟 → 宁可放过
      补充：丢错 = 退化成短回复，**安全**；放过 = 废话上线丢人 → 宁可丢掉
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app as app_mod                      # noqa: E402
from engine import style as style_engine   # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 88)
    print("两段式补充：判定工具与配置")
    print("=" * 88)

    wso = app_mod._worst_sentence_overlap

    # ① 逐句判定：**整段会比不出来的一小句复述**，逐句要能抓到
    main_reply = "嗯，我懂。你想说什么都行。"
    sup_diluted = "你说的我都记着呢。其实想说什么都行这话我说过。"
    check(wso(main_reply, sup_diluted) >= 0.3,
          "**整段会被稀释的一小句复述，逐句能抓到**",
          f"最坏一句 {wso(main_reply, sup_diluted):.2f}")

    # ② 真新内容 → 不该判重复
    fresh = "要不先吃点东西？饿着更难受。"
    check(wso(main_reply, fresh) < 0.3, "真新内容不判重复",
          f"{wso(main_reply, fresh):.2f}")

    # ③ 空串 / 空白不炸
    check(wso(main_reply, "") == 0.0, "空补充不报错")

    # ④ 补充剪编造：剪光要返回空串（**不能**返回兜底句）
    cand, hit = style_engine.strip_claims(
        "你说过话我就听着；", ("你说过",), fallback="")
    check(hit and cand == "", "补充剪光返回空串（不接兜底句）", f"→ {cand!r}")

    # ⑤ 主回复剪光仍给兜底（两种调用方行为不同，别弄混）
    cand2, hit2 = style_engine.strip_claims("你上次也这么说的。", ("上次也",))
    # ⚠ 兜底句现在从数组**随机取一句**，不能断言固定子串「记不」
    #   （新变体里有两句不含它 → 会 2/3 概率误报）。断言「是候选之一且非空」。
    from engine import replies as _R
    check(hit2 and cand2 in _R.all_of("no_memory"),
          "主回复剪光给安全兜底（候选之一）", f"→ {cand2}")

    # ⑥ **配置默认必须是关**——实验特性不能悄悄上线
    cfg = (style_engine.load_style().get("supplement") or {})
    check(cfg.get("enabled") is False, "**supplement 默认关**", str(cfg.get("enabled")))
    for key in ("prob", "echo_th", "judge", "max_chars"):
        check(key in cfg, f"配置项 {key} 在")

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
