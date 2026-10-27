"""钉住「同一个模型服务多个角色」的防呆（**不连模型、不起服务**）。

## 用户提的规则

「当在设置中设置任务为同一个模型完成，不要多次启动，
  相当于用同一个模型完成多种工作」

**为什么这不是小事**：判官用 9B 最准（召回 100%），用户很可能把判官也设成 9B。
不做防呆就会起**两个 9B 实例 = 白白多吃 5.2 GB**，
而四个常驻本来只剩零点几 GB（实测五个全上只剩 0.03 GB，会换页）。

## 钉什么

  ① 同一个 .gguf → `model_key` 相同（**按路径判，不按关键词**：
     两个角色可能写不同关键词却指向同一个文件）
  ② `shared_with(role)` 指向那个已经在跑的角色
  ③ `effective_port(role)` 返回**对方的**端口
  ④ `is_served(role)` 在共用时也是 True（界面不该显示「没跑」）
  ⑤ **embed 永远不共用**（它给的是 /v1/embeddings，和 chat 不是一回事）
  ⑥ 不同模型时**不许**误判为共用

⚠ 这里只测"判定逻辑"；"真的只起一个进程"由
  `tests/analysis/shared_role_probe.py` 实起服务验证。
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
    print("共用模型防呆（判定逻辑）")
    print("=" * 88)

    # ---- ① CHAT_ROLES 不含 embed ----
    check("embed" not in mc.CHAT_ROLES, "**embed 不在可共用的角色里**",
          str(mc.CHAT_ROLES))
    check(set(mc.CHAT_ROLES) == {"chat", "memory", "think", "judge"},
          "可共用的就是四个对话类角色")

    # ---- ② model_key 按**真实路径**判（不是关键词）----
    keys = {r: mc.model_key(r) for r in mc.ROLES}
    check(all(keys.values()), "每个角色的 model_key 都能算出来")
    check(keys["judge"] != keys["think"],
          "现在判官(4B)和思考(9B)不是同一个文件（所以不该判为共用）",
          f"judge={keys['judge'][-28:]}")
    # path 形式（统一小写、正斜杠）——避免同一个文件因写法不同被判成两个
    check(all("\\" not in k for k in keys.values()),
          "model_key 统一成正斜杠（同一文件不该因写法不同被当成两个）")

    # ---- ③ 默认配置下没人共用 ----
    for role in mc.CHAT_ROLES:
        check(mc.shared_with(role) is None,
              f"默认配置下 {mc.ROLE_LABELS[role]} 没有共用对象",
              str(mc.shared_with(role)))
    for role in mc.ROLES:
        check(mc.effective_port(role) == mc.role_port(role),
              f"没共用时 {mc.ROLE_LABELS[role]} 用自己端口", str(mc.effective_port(role)))

    # ---- ④ 造一个"判官=思考"的情形，验证判定与恢复 ----
    orig_judge = str((mc.load()["roles"]["judge"] or {}).get("model") or "")
    think_model = str((mc.load()["roles"]["think"] or {}).get("model") or "")
    try:
        mc.set_role_param("judge", "model", think_model)
        check(mc.model_key("judge") == mc.model_key("think"),
              "改完之后两个角色 model_key 相同")
        # ⚠ 没起服务时 shared_with 应该是 None（它要求对方**真的在跑**）
        check(mc.shared_with("judge") is None,
              "**对方没在跑时不算共用**（只看配置不动手）",
              str(mc.shared_with("judge")))
        # 模拟"思考模型在跑"：只替换存活判定
        orig_alive = mc._alive
        try:
            mc._alive = lambda port, timeout=1.5: int(port) == mc.role_port("think")
            check(mc.shared_with("judge") == "think",
                  "**思考模型在跑时，判官判定为共用 think**",
                  str(mc.shared_with("judge")))
            check(mc.effective_port("judge") == mc.role_port("think"),
                  "**effective_port 指向 8085，不是 8084**",
                  str(mc.effective_port("judge")))
            check(mc.is_served("judge") is True,
                  "**共用时 is_served 也算 True**（界面不该显示「没跑」）")
            ep = mc.judge_endpoint()
            check(bool(ep and str(mc.role_port("think")) in ep),
                  "**judge_endpoint 直接给出共用端口**（不另起一份）", str(ep))
        finally:
            mc._alive = orig_alive
    finally:
        mc.set_role_param("judge", "model", orig_judge)

    check(str((mc.load()["roles"]["judge"] or {}).get("model")) == orig_judge,
          "配置已复原")


    # ---- ⑤ **端到端路由**：四个角色真的把活交到同一个服务上 ----
    #
    # ⚠ 用户指出的关键点：「万一有人极端一点的用同一个模型完成记忆、对话、
    #   思考和判官的话，**工作要能路由到**才行」。
    #   只做"不重复启动"而没把活路由过去，结果是抽取还在打空的 8083、
    #   思考还在打空的 8085、判官还在打空的 8084 —— **一个都没连上，功能全废**，
    #   而且看起来像是"模型不行"。
    from cognition.extractor import extract_url
    from cognition.vectors import embed_url
    from services import think_slot as _ts

    orig = {r: str((mc.load()["roles"][r] or {}).get("model") or "")
            for r in ("memory", "think", "judge")}
    chat_model = str((mc.load()["roles"]["chat"] or {}).get("model") or "")
    orig_alive2 = mc._alive
    try:
        for r in ("memory", "think", "judge"):
            mc.set_role_param(r, "model", chat_model)
        # 模拟「只有对话那一个服务在跑」
        chat_port = mc.role_port("chat")
        mc._alive = lambda port, timeout=1.5: int(port) == chat_port
        want = str(chat_port)
        check(want in mc.endpoint("memory"), "**endpoint(memory) → 对话端口**",
              mc.endpoint("memory"))
        check(want in mc.endpoint("think"), "**endpoint(think) → 对话端口**",
              mc.endpoint("think"))
        check(want in mc.endpoint("judge"), "**endpoint(judge) → 对话端口**",
              mc.endpoint("judge"))
        check(want in _ts.url(), "**think_slot.url() → 对话端口**", _ts.url())
        try:
            eu = extract_url()
        except Exception as exc:
            eu = f"出错 {exc}"
        check(want in eu, "**extract_url() → 对话端口**（抽取真的连得上）", eu)
        # 共用关系
        for r in ("memory", "think", "judge"):
            check(mc.shared_with(r) == "chat", f"shared_with({r}) = chat",
                  str(mc.shared_with(r)))
        # 四个角色都报可用
        rr = mc.running_roles()
        check(all(rr.get(x) for x in ("chat", "memory", "think", "judge")),
              "**四个角色都报可用**（一个服务全包）")
        # **嵌入不许被并进去**：它给的是 /v1/embeddings
        check(str(mc.role_port("embed")) in embed_url(),
              "**嵌入仍走自己的端口**（embeddings 不能共用）", embed_url())
    finally:
        mc._alive = orig_alive2
        for r, m in orig.items():
            mc.set_role_param(r, "model", m)

    print()
    print("=" * 88)
    if FAIL:
        print(f"✗ {len(FAIL)} 项不符：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
