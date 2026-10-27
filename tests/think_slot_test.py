"""钉住「思考模型按需起停」的规则（不连模型、不起服务）。

## 这套逻辑的四个易错点

  ① **档位门槛**：起模型要 **10~15 秒**（实测 17 秒），所以只许**深档**触发。
     l1 的日常闲聊绝不能为它等十几秒——一旦门槛写错，每一句"今天好累"都会卡住。
  ② **开关分两处**：`auto_think.enabled`（要不要自动起）和 `auto_think.mode`
     （起了之后要不要让难题真的交给它答）是**两件事**。
     而且界面复选框走 `switch()`，程序读 `auto_think.enabled` —— 两边必须指向同一处
     （`models_config._ALIAS` 就是干这个的；不对齐会"勾了没反应"）。
  ③ **只关自己起的**：用户手动开的 9B 不许被空闲回收抢走。
  ④ **mode 取值必须收紧**：写错一个词（比如 "generat"）不能悄悄变成"接管回答"，
     要退回最安全的 `judge_only`。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services import models_config as mc   # noqa: E402
from services import think_slot as ts      # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 88)
    print("思考模型按需起停")
    print("=" * 88)

    # ⚠ 出厂默认是 **false**（起模型实测 17 秒，不能默认开）。
    #   测试结束**显式还原成 false**，而不是"还原到进来时的样子"——
    #   后者会让一次手动实验（把开关打开）永久留在配置里，之后每次跑测试都失败。
    orig = False
    # **在任何改动之前**记下出厂值——测试中途会把开关打开，
    # 到收尾再去读就永远是 True 了（踩过）。
    shipped_enabled = mc.switch("auto_think")
    try:
        # ---- ① 开关一致（界面写的地方 == 程序读的地方）----
        mc.set_switch("auto_think", True)
        check(mc.switch("auto_think") is True, "开关 True 能读回")
        check(ts.enabled() is True, "**think_slot.enabled() 和 switch() 是同一处**",
              f"switch={mc.switch('auto_think')} enabled={ts.enabled()}")
        mc.set_switch("auto_think", False)
        check(ts.enabled() is False, "开关 False 时 think_slot 也认为关")

        # ---- ② 关着的时候任何档位都不触发 ----
        for lv in ("l0", "l1", "l2", "l3"):
            check(ts.should_trigger(lv) is False,
                  f"关了 → {lv} 不触发")

        # ---- ③ 开着的时候只有深档触发 ----
        mc.set_switch("auto_think", True)
        check(ts.should_trigger("l0") is False, "**l0 不触发**（闲聊）")
        check(ts.should_trigger("l1") is False, "**l1 不触发**（日常，别为它等 17 秒）")
        check(ts.should_trigger("l2") is True, "l2 触发")
        check(ts.should_trigger("l3") is True, "l3 触发")

        # ---- ④ mode 取值收紧 ----
        # 2026-10-21 补 `transfer`（9B 出内容、v1.5 转述）——用户定的方案
        check(ts.mode() in ("judge_only", "generate", "transfer", "off"),
              "mode 取值合法（含 transfer）", ts.mode())
        orig_mode = ts.mode()
        try:
            # ⚠ 用 `set_value` **行级**改，不要 `yaml.safe_dump` 整份写——
            #   后者会把 models.yaml 里的实测记录（注释）全抹掉，踩过两次。
            mc.set_value("auto_think", "mode", "generat")
            check(ts.mode() == "judge_only",
                  "**写错的 mode 退回 judge_only**（不许悄悄接管回答）", ts.mode())
        finally:
            mc.set_value("auto_think", "mode", orig_mode)

        # ---- ⑤ mode=off 时不触发 ----
        try:
            mc.set_value("auto_think", "mode", "off")
            check(ts.should_trigger("l3") is False, "mode=off → 不触发")
        finally:
            mc.set_value("auto_think", "mode", orig_mode)

        # ---- ⑥ **只关自己起的**（手动开的不许被空闲回收抢走）----
        check(ts.stop() is False,
              "**没在我们记录里的模型 → stop() 拒绝动手**",
              "（现在什么都没有，返回 False 即正确）")

        # ---- ⑦ 空闲阈值可读且为正 ----
        check(ts.idle_minutes() > 0, "idle_minutes 为正", str(ts.idle_minutes()))

        # ---- ⑧ 默认值（有 17 秒代价的功能不能默认开）----
        #   读原始配置（不是本次测试改过的）来验证
        raw = (mc.load().get("auto_think") or {})
        check("enabled" in raw, "配置里有 auto_think.enabled 这一项")
        # ⚠ **默认必须还是 enabled=false**：起模型实测 17 秒，不能悄悄开。
        #   （mode 的用户选择是 transfer —— 9B 出内容、v1.5 转述；
        #    但 enabled=false 时整条链路都不会触发，所以这个默认是安全的。）
        check(shipped_enabled is False,
              "**出厂默认 auto_think.enabled = false**（17 秒代价，不默认开）",
              f"运行前读到的值={shipped_enabled}")
        check(str(raw.get("mode")) == "transfer",
              "默认 mode 是用户定的 transfer（9B 出内容 / v1.5 转述）",
              str(raw.get("mode")))
        # 兜底方向仍然是 judge_only（写错任何东西都退回最安全那个）
        check(ts.mode() != "generate",
              "**默认不把回答直接交给 9B**（不走 generate）", ts.mode())
    finally:
        mc.set_switch("auto_think", orig)

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
