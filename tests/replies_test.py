"""钉住「固定回复由一个文件维护」这条契约。

## 为什么值得一条测试

用户的要求是：**改 `config/replies.yaml` 就能改未央说的话，不用动代码**。
这条契约有两个容易破的方向：

  ① 文件改了但代码没读到（缓存不失效 / 键名对不上）→ 用户以为改了，其实没生效
  ② 文件写坏 / 被删 → 未央"说不出话"，或者程序直接崩

所以这里**两头都测**：
  · 改了文件，**下一次取就必须是新值**（按 mtime 热加载）
  · 文件坏掉/删掉，**必须回退到内置默认**，不能抛异常

⚠ 测试会真的改 `config/replies.yaml`，结束时**原样还原**。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine import replies as R  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 88)
    print("固定回复文件（config/replies.yaml）")
    print("=" * 88)

    path = R.ensure_file()
    check(path.exists(), "文件存在（缺了会自动生成）", str(path.name))
    original = path.read_text(encoding="utf-8")

    try:
        # ---- ① 默认值是**数组**，且首项就是搬走前代码里的原话 ----
        pool = R.all_of("no_memory")
        check(len(pool) >= 1 and pool[0] == "这个我记不太清了，你再跟我说说？",
              "no_memory 是数组，首项保持原话", f"{len(pool)} 项")
        check(all(isinstance(x, str) and x.strip() for x in pool),
              "数组里每项都是非空字符串")
        check(len(R.refuse_list("soft")) == 2, "soft 拒绝话术 2 条")
        for kind in ("soft", "hard", "transfer", "safety"):
            check(bool(R.refuse_list(kind)), f"refuse.{kind} 非空",
                  str(len(R.refuse_list(kind))) + " 条")
        check(any("这件事我还没有能耐" in x for x in R.all_of("no_skill")),
              "no_skill 有内容", f"{len(R.all_of('no_skill'))} 项")

        # ---- ② 改文件 → 立刻生效（mtime 热加载）----
        #
        # ⚠ 这里**整份重写成最小文件**，而不是在原文件里插入一行。
        #   第一版是 `original.replace("refuse:", 'refuse:\n  soft: [...]')`，
        #   结果文件里出现**两个 `refuse:` 键** —— YAML 的 `safe_load` 遇到
        #   重复键取**最后一个**，于是原话术赢了，测试报"没生效"（其实是测试写错）。
        #   最小文件还有个附带好处：顺带验证了"缺的键会用内置默认补上"。
        path.write_text(
            'no_memory: "【测试】我真的不记得了"\n'
            "refuse:\n"
            '  soft: ["【测试】软拒绝一句"]\n',
            encoding="utf-8")
        got = R.get("no_memory")
        check(got == "【测试】我真的不记得了",
              "**改文件后立刻生效**（不用重启）", got)
        soft = R.refuse_list("soft")
        check(soft and soft[0] == "【测试】软拒绝一句",
              "**改拒绝话术也生效**", str(soft[:1]))
        # ---- ③ 文件里没写的键 → 用内置默认补（不能变空）----
        check(len(R.refuse_list("hard")) == 2,
              "缺的键回退到内置默认（hard 仍是 2 条）",
              str(len(R.refuse_list("hard"))))
        check(bool(R.get("greeting")), "缺的 greeting 也有值", R.get("greeting")[:12])

        # ---- ④ 数组语义：随机取 + 能取到每一项 ----
        path.write_text(
            "no_memory:\n"
            '  - "A 句"\n'
            '  - "B 句"\n'
            '  - "C 句"\n',
            encoding="utf-8")
        seen = {R.get("no_memory") for _ in range(120)}
        check(seen == {"A 句", "B 句", "C 句"},
              "**从数组里随机取，且每项都可能被取到**", str(sorted(seen)))
        check(len({R.get("no_memory") for _ in range(30)}) > 1,
              "**同一键连续取会变**（不是永远第一句）")

        # ---- ⑤ 只写一句时不报错，也不用随机 ----
        path.write_text('no_memory:\n  - "只有一句"\n', encoding="utf-8")
        check({R.get("no_memory") for _ in range(10)} == {"只有一句"},
              "数组只有一项时稳定返回那一句")

        # ---- ⑥ 写成裸字符串也能用（老文件/手写兼容）----
        path.write_text('no_memory: "裸字符串一句"\n', encoding="utf-8")
        check(R.get("no_memory") == "裸字符串一句",
              "裸字符串写法兼容（等价于只有一项的数组）")

        # ---- ④ 文件写坏 → 回退到默认，不抛异常 ----
        path.write_text("这是一个坏的 yaml: [[[ ::: \n\t- x", encoding="utf-8")
        R._cache["mtime"] = None          # 确保重新读
        try:
            bad = R.get("no_memory")
            ok = bool(bad)
        except Exception as exc:
            ok, bad = False, f"{type(exc).__name__}: {exc}"
        check(ok, "**坏文件不崩，回退到默认**", str(bad)[:30])

        # ---- ⑤ 文件被删 → 自动重建 ----
        path.unlink()
        R._cache["mtime"] = None
        rebuilt = R.ensure_file()
        check(rebuilt.exists(), "**文件被删会自动重建**")

        # ---- ⑥ 调用点真的用了 replies（而不是还硬编码）----
        import engine.refuse as refuse
        check(len(refuse._GENERIC.get("soft") or []) == 2,
              "refuse._GENERIC 从 replies 取")
        check(refuse.NO_SKILL in R.all_of("no_skill"),
              "refuse.NO_SKILL 是候选之一", refuse.NO_SKILL[:16])
        # ⚠ `no_skill()` 必须**每次现取**：若返回导入时常量，数组就白配了
        got_ns = {refuse.no_skill() for _ in range(60)}
        check(len(got_ns) > 1,
              "**no_skill() 每次随机取**（不是进程内固定一句）", str(len(got_ns)) + " 种")
        from engine.style import strip_claims
        out, cut = strip_claims("我上次说过这个，你记得吧。", ("上次",))
        check(cut and out in R.all_of("no_memory"),
              "**strip_claims 兜底句来自 replies**", out[:24])

        import gui
        # ⚠ 两边都是**随机取一句**，不能互相比较（必然偶发不等）
        check(gui._greet() in R.all_of("greeting"), "gui._greet 来自 replies",
              gui._greet()[:16])
        check(gui._greet_ja() in R.all_of("greeting_ja"), "gui._greet_ja 来自 replies")
        import gui_theme
        check(gui_theme._placeholder() in R.all_of("placeholder_reply"),
              "gui_theme._placeholder 来自 replies",
              gui_theme._placeholder()[:16])
        check(gui_theme._reply_word("listening", "x") in R.all_of("listening"),
              "gui_theme._reply_word 来自 replies")
    finally:
        path.write_text(original, encoding="utf-8")
        R._cache["mtime"] = None
        R.load()
    check(R.all_of("no_memory")[0] == "这个我记不太清了，你再跟我说说？",
          "已还原原始文件", f"{len(R.all_of('no_memory'))} 项")

    # ⚠ **文件里的转义必须能原样还原**：话术带换行时最容易悄悄改味
    #   （YAML 双引号标量把字面换行折成空格，不报错、几乎看不出来）。
    bad = R.verify_roundtrip()
    check(not bad, "**写出去再读回来一字不差**（换行没被折成空格）", str(bad))
    # 默认话术里**确实有带换行的**，否则上面那条等于没测
    check(any(chr(10) in x for x in R._DEFAULTS["no_skill"]),
          "默认话术里有带换行的句子（确保上面确实测到了换行）")

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
