"""钉住「五个角色的模型配置」和「判官只能用 9B / 4B」这两条规则。

## 为什么这两条要钉

用户定的规则很清楚，但很容易在后续改动里被无意破坏：

  ① **判官只能是 think(9B) 或 judge(4B)**，**不许**用 memory(2B)
     实测同一批 12 例（专门挑"程序抓不到"的编造）：
         程序侧（数字+编前情）   抓到 0/6
         2B 兼职判官             召回 **50%**  ✘ 漏「你孩子上小学了吗」这种
         4B                     召回 83%
         9B                     召回 **100%**
     真的用 2B 当判官，等于把第三道门做成半掩的。

  ② **9B 在就用 9B，不看 4B**（用户明确要求"这样 9b 在就不用 4b 了"）
  ③ **两个都不在 → 返回 None**（调用方跳过判定），
     而不是退到随便一个模型、也不是硬起一个占 5 GB 的。
  ④ 五个角色都必须能**解析到真实存在的模型文件**（配置错了要当场暴露）。

不连模型、不起服务。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services import models_config as mc   # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if cond else '✗'} {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAIL.append(label)


def main() -> int:
    print("=" * 88)
    print("五个角色 + 判官规则")
    print("=" * 88)

    cfg = mc.load()

    # ---- ① 五个角色齐全 ----
    check(set(mc.ROLES) <= set(cfg["roles"]), "五个角色都在", str(list(mc.ROLES)))
    for role in mc.ROLES:
        item = cfg["roles"][role]
        check(bool(item.get("model")), f"{mc.ROLE_LABELS[role]} 配了模型",
              str(item.get("model")))
        check(int(item.get("port") or 0) > 0, f"{mc.ROLE_LABELS[role]} 有端口",
              str(item.get("port")))

    # ---- ② 每个角色都能解析到真实文件（配置写错要当场炸） ----
    #
    # ⚠ 2026-10-27：模型文件是**部署产物**，不进公开仓库。
    #   一个都没有时 → 整条跳过（并说清楚原因），而不是判失败。
    #   有任何一个 → 仍然逐个严格验（配置写错必须当场炸）。
    # ⚠⚠ 坑：`pathlib.Path("")` 等于 `Path(".")`，`.exists()` **永远是 True**。
    #   第一版写 `Path(str(resolve_model_path(r) or "")).exists()`，
    #   于是"一个模型都没有"也被判成"有模型"，断言照样执行、照样全红。
    #   → 先判空，再判存在。
    _resolved = {r: mc.resolve_model_path(r) for r in mc.ROLES}
    _any_model = any(bool(p) and pathlib.Path(str(p)).exists()
                     for p in _resolved.values())
    if not _any_model:
        print("  ⏭ 跳过「每个角色都能解析到真实文件」：本机没有扫描到任何模型文件"
              "（公开仓库不含模型，用 COMPANION_MODEL_DIRS 指向你的模型目录）")
    for role in mc.ROLES:
        if not _any_model:
            break
        path = mc.resolve_model_path(role)
        ok = bool(path) and pathlib.Path(str(path)).exists()
        check(ok, f"{mc.ROLE_LABELS[role]} 解析到真实文件",
              str(path).split("\\")[-1] if path else "None")

    # ---- ③ **判官规则（最要紧）** ----
    check(tuple(cfg.get("judge_prefer") or []) == ("think", "judge")
          or list(cfg.get("judge_prefer") or []) == ["think", "judge"],
          "**judge_prefer 顺序是 think → judge（9B 优先）**",
          str(cfg.get("judge_prefer")))
    prefer = list(cfg.get("judge_prefer") or [])
    check(prefer[:1] == ["think"], "**9B 排在 4B 前面**（9B 在就不用 4B）", str(prefer))
    check("memory" not in prefer, "**2B 不在判官候选里**（召回只有 50%）")
    check(set(prefer) <= set(mc.JUDGE_OK), "判官候选都在 JUDGE_OK 内",
          f"{prefer} ⊆ {list(mc.JUDGE_OK)}")
    check("memory" in (cfg.get("judge_forbidden") or []),
          "memory 明确列在 judge_forbidden 里")
    check(mc.JUDGE_OK == ("think", "judge"), "JUDGE_OK 常量没被改宽",
          str(mc.JUDGE_OK))

    # ---- ④ 没有模型在跑时必须返回 None（跳过判定，不许乱退） ----
    running = mc.running_roles()
    if not any(running.values()):
        check(mc.judge_endpoint() is None,
              "**一个都没在跑 → 判官返回 None**（跳过判定，不硬起模型）")
    else:
        ep = mc.judge_endpoint()
        up = [r for r, v in running.items() if v]
        check(ep is not None or not ({"think", "judge"} & set(up)),
              "有判官角色在跑时才给端点", f"在跑={up}　端点={ep}")

    # ---- ⑤ **开关**（用户 2026-10-21 要求做成可配置） ----
    for key in mc.DEFAULT_SWITCHES:
        check(isinstance(mc.switch(key), bool), f"开关 {key} 可读", str(mc.switch(key)))

    # **机会主义判官开/关，选的必须是不同角色**
    #   开 → 有思考模型就用思考模型
    #   关 → **只用「判官」那一行选的模型**，不管思考模型在不在
    orig_alive, orig_switch = mc._alive, mc.switch
    try:
        mc._alive = lambda port, timeout=1.5: int(port) in (8084, 8085)
        mc.switch = lambda name, _o=orig_switch: (
            False if name == "opportunistic_judge" else _o(name))
        off_ep = mc.judge_endpoint()
        mc.switch = lambda name, _o=orig_switch: (
            True if name == "opportunistic_judge" else _o(name))
        on_ep = mc.judge_endpoint()
    finally:
        mc._alive, mc.switch = orig_alive, orig_switch
    check(off_ep and off_ep.endswith(":8084/") or (off_ep and "8084" in off_ep),
          "**机会主义关 → 只用判官模型（8084）**", str(off_ep))
    check(on_ep and "8085" in on_ep,
          "**机会主义开 → 用思考模型（8085）**", str(on_ep))

    # ---- ⑥ 端口不冲突（五个角色各占各的） ----
    ports = [int(cfg["roles"][r].get("port") or 0) for r in mc.ROLES]
    check(len(set(ports)) == len(ports), "五个角色端口互不冲突", str(ports))


    # ---- ⑦ **解析失败时绝不许把默认值写回文件** ----
    #
    # ⚠ 这条是踩出来的：配置里混进一个 BEL 控制字符 → yaml 解析失败
    #   → `load()` 静默返回内置默认值 → 之后任何 `set_role_param` 走
    #   `_write_all(默认值)` → **memory/think/judge 的模型全被清空**。
    #   用户的配置被自己的程序悄悄抹掉，是最难查的一类事故。
    import shutil as _shutil
    import tempfile as _tmp
    import yaml as _yaml
    original = mc.CONFIG_PATH.read_text(encoding="utf-8")
    try:
        # 造一个坏文件（混进控制字符）
        mc.CONFIG_PATH.write_text(
            original.replace("roles:", "roles:\x07", 1), encoding="utf-8")
        mc._LOAD_CACHE["mtime"] = None          # 让缓存失效
        bad_load = mc.load()
        check(bool(mc._PARSE_ERROR), "坏配置能被识别（记下解析错误）",
              mc._PARSE_ERROR[:50])
        refused = False
        try:
            mc.set_role_param("chat", "model", "should-not-be-written")
        except Exception:
            refused = True
        check(refused, "**解析失败时 set_role_param 拒绝写入**")
        after = mc.CONFIG_PATH.read_text(encoding="utf-8")
        check("should-not-be-written" not in after,
              "**坏文件没有被覆盖写回**（内容保持原样）")
        check(any(c in after for c in ("\x07",)), "坏文件原样保留（等用户自己修）")
    finally:
        mc.CONFIG_PATH.write_text(original, encoding="utf-8")
        mc._LOAD_CACHE["mtime"] = None
        mc.load()
    check(mc._PARSE_ERROR == "", "恢复好文件后解析状态清空")

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
