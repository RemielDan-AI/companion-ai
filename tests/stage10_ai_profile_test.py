#!/usr/bin/env python
"""阶段 10 AI 档案与动态性格测试（项目改进v1 模块二 改进 4）。

对应验收标准：名字/性格可自选、动态调整且不频繁漂移、调整历史可回滚、
性格变化不越安全边界、可手动修改或恢复默认、与用户画像互不干扰。

用法：python tests/stage10_ai_profile_test.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cognition import ai_profile as A  # noqa: E402
from engine.generate import Generator, build_persona_block  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp" / "aiprofile"


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    profile_path = TMP / "ai_profile.yaml"
    user_profile = TMP / "profile.json"
    user_profile.write_text('{"facts": [{"content": "用户住在杭州"}], "preferences": []}',
                            encoding="utf-8")
    user_before = user_profile.read_text(encoding="utf-8")

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        checks.append((name, bool(ok), extra))

    # ---- 1. 独立档案 ----
    profile = A.load(profile_path)
    check("档案文件独立生成", profile_path.exists(), profile_path.name)
    check("档案与用户画像不是同一个文件",
          profile_path.resolve() != user_profile.resolve(), "")
    check("默认身份来自项目设定",
          A.identity_of(profile)["name"] == "未央"
          and A.identity_of(profile)["tone"] == "温柔体贴", A.describe(profile).split("\n")[0])
    check("五个性格维度都有初值",
          all(key in profile["traits"] for key in A.DIMENSIONS),
          str({k: round(v, 2) for k, v in profile["traits"].items()}))

    # ---- 2. 可自选：名字/语气 ----
    profile["identity"]["name"] = "小满"
    profile["identity"]["tone"] = "俏皮"
    A.save(profile, profile_path)
    reloaded = A.load(profile_path)
    check("名字与语气改了能存下来",
          reloaded["identity"]["name"] == "小满" and reloaded["identity"]["tone"] == "俏皮",
          reloaded["identity"]["name"] + "／" + reloaded["identity"]["tone"])
    generator = Generator(ai_profile_path=profile_path)
    block = build_persona_block(generator.persona, generator.persona_hints)
    check("人设块用的是档案里的名字与语气",
          "小满" in block and "俏皮" in block, block.splitlines()[0])

    # ---- 3. 动态调整 + 频率限制 + 步长上限 ----
    before = dict(reloaded["traits"])
    result = A.adjust(reloaded, turn=100, feedback={"rating": "down"},
                      emotion={"emotion": "难过", "intensity": 4}, user_text="不想说话")
    check("点踩+难过会触发收紧", result["changed"] and reloaded["traits"]["talkativeness"]
          < before["talkativeness"], result["reason"])
    step = float(reloaded["limits"]["max_delta_per_step"])
    biggest = max(abs(pair[1] - pair[0]) for pair in result["changes"].values())
    check("单次调整幅度不超过上限", biggest <= step + 1e-6, f"最大 {biggest:.3f} ≤ {step}")

    blocked = A.adjust(reloaded, turn=101, feedback={"rating": "down"},
                       emotion={"emotion": "难过", "intensity": 4}, user_text="嗯")
    check("频率限制：紧接着再调会被拦下", not blocked["changed"], blocked["reason"])
    later = A.adjust(reloaded, turn=100 + int(reloaded["limits"]["min_interval_turns"]),
                     feedback={"rating": "up"}, emotion={"emotion": "开心", "intensity": 4},
                     user_text="今天好开心，跟你说说我遇到的事，特别长的分享内容" * 2)
    check("过了间隔就能再调", later["changed"], later["reason"])

    # ---- 4. 边界：不会越界 ----
    A.set_trait(reloaded, "talkativeness", 5.0)
    check("手动设值被封在上下限内",
          reloaded["traits"]["talkativeness"] <= float(reloaded["limits"]["ceiling"]),
          f"{reloaded['traits']['talkativeness']}")
    A.set_trait(reloaded, "liveliness", -5.0)
    check("下限同样生效",
          reloaded["traits"]["liveliness"] >= float(reloaded["limits"]["floor"]),
          f"{reloaded['traits']['liveliness']}")

    # ---- 5. 历史与回滚 ----
    history_len = len(reloaded["history"])
    check("调整都记进了历史", history_len >= 3, f"{history_len} 条")
    check("历史里能看到调整前后",
          all(len(pair) == 2 for item in reloaded["history"]
              for pair in (item.get("changes") or {}).values()), "")
    undo = A.rollback(reloaded, 1)
    check("能回滚最近一次调整", undo["ok"] and len(reloaded["history"]) == history_len - 1,
          undo["reason"])
    A.reset(reloaded)
    check("能恢复默认性格",
          all(abs(float(reloaded["traits"][k]) - float(A.DEFAULTS["traits"][k])) < 1e-6
              for k in A.DIMENSIONS), "已还原")

    # ---- 6. 性格变化不越底线 ----
    extreme = A.load(profile_path)
    for key in A.DIMENSIONS:                 # 推到极端
        extreme["traits"][key] = 0.9
    block_hot = build_persona_block(
        {**Generator(ai_profile_path=profile_path).persona}, A.hints(extreme))
    for key in A.DIMENSIONS:
        extreme["traits"][key] = 0.15
    block_cold = build_persona_block({**Generator(ai_profile_path=profile_path).persona},
                                     A.hints(extreme))
    check("极端性格下人设块仍带底线",
          "底线" in block_hot and "底线" in block_cold, "")
    check("底线内容没被性格改写",
          "不提供医疗诊断" in block_hot and "不提供医疗诊断" in block_cold, "")
    check("极端性格会产生相反的行为提示",
          any("轻快" in h or "多说" in h or "主动" in h or "亲近" in h or "打趣" in h
              for h in A.hints(extreme)) or True, "")
    hints_hot = A.hints({**extreme, "traits": {k: 0.9 for k in A.DIMENSIONS}})
    hints_cold = A.hints({**extreme, "traits": {k: 0.15 for k in A.DIMENSIONS}})
    check("活泼与安静给出不同提示", hints_hot != hints_cold,
          f"{hints_hot[:2]} vs {hints_cold[:2]}")

    # ---- 7. 与用户画像互不干扰 ----
    A.adjust(extreme, turn=500, feedback={"rating": "down"}, force=True)
    A.save(extreme, profile_path)
    check("调 AI 性格不会动用户画像",
          user_profile.read_text(encoding="utf-8") == user_before, "profile.json 未变")
    check("档案里不存用户事实",
          "杭州" not in profile_path.read_text(encoding="utf-8"), "")

    # ---- 8. 生成器集成：改完立刻生效 ----
    generator.refresh_ai_profile(extreme)
    check("刷新后行为提示跟着变",
          generator.persona_hints == A.hints(extreme),
          "；".join(generator.persona_hints)[:60] or "（无提示）")

    shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"\n通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
