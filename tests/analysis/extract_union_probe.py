"""量一件事：**抽两次取并集，能救回多少"模型抖动"造成的漏抽**？

## 背景

`extract_metric.py` 定位出：抽取器剩下的毛病**主要是抖动**——
同一句「我妹妹不吃香菜」，有时抽出 `妹妹不吃香菜`（对），
有时只抽出个没主语的 `不吃香菜`（被主体校验正确挡掉）→ **这条就没了**。

抖动是**模型**的问题，但不改模型也有程序侧的办法：
**抽两次取并集**——两次里只要有一次抽对，就能救回来。
代价是抽取从 ~2.3s 变成 ~4.6s（抽取发生在回复之后，用户无感）。

## 但必须先量，不能凭直觉

两个风险：
    · 并集会不会**引入编造**（两次各自多抽一点，合起来更脏）
    · 提升到底有多少（值不值 2 倍耗时）

本探针就量这两个。判据用同一套金标准（与 extract_metric.py 一致）。

用法：
    python tests/model_server.py run --model Qwen3.5-2B --ngl 99 \
        --script tests/analysis/extract_union_probe.py
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

# ⚠ 固定跑 6 次：前 3 次按"单次"算，两两配成 3 个"并集对"，样本量才够比
ROUNDS = max(4, int(os.environ.get("UNION_ROUNDS", "6")))

GOLD = [
    ("我妹妹叫林小雨", "林小雨", ""),
    ("林小雨对花生过敏", "林小雨", "花生"),
    ("我妹妹对海鲜过敏", "妹妹", "海鲜"),
    ("我妹妹不吃香菜", "妹妹", "香菜"),
    ("我住在南京", "南京", ""),
    ("我已经搬到成都了", "成都", ""),
    ("我养了只猫叫团子", "团子", ""),
    ("团子三岁了", "团子", "三岁"),
    ("我同事陈思远做后端", "陈思远", "后端"),
    ("小花是我妹妹的同学", "小花", ""),
    ("我妹妹在广州大学上学", "广州大学", "广州大学"),
    ("我妈在杭州住院了", "杭州", "住院"),
]


def _blob(info: dict) -> str:
    parts = []
    for k in ("facts", "preferences", "relations"):
        parts += [str(x) for x in (info.get(k) or [])]
    return " ".join(parts)


def _hit(info: dict, subject: str, value: str) -> bool:
    blob = _blob(info)
    return (not subject or subject in blob) and (not value or value in blob)


def _union(a: dict, b: dict) -> dict:
    """把两次抽取按字段并起来（去重）。"""
    out = {}
    for k in ("facts", "preferences", "relations"):
        seen, merged = set(), []
        for item in list(a.get(k) or []) + list(b.get(k) or []):
            s = str(item)
            if s not in seen:
                seen.add(s)
                merged.append(s)
        out[k] = merged
    return out


def _invented(info: dict, user_text: str) -> int:
    n = 0
    for k in ("facts", "preferences"):
        for item in (info.get(k) or []):
            body = EX._canonical(k[:-1], str(item))
            for pfx in ("对方提到的人：", "对方", "用户"):
                if body.startswith(pfx):
                    body = body[len(pfx):]
                    break
            if not EX._grounded(body, user_text):
                n += 1
    return n


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    gen = Generator(api_url=url)
    print(f"并集实验　模型：{os.environ.get('EXTRACT_MODEL_TAG') or url}"
          f"　每句 {ROUNDS} 次")
    print("=" * 96)

    single_ok = union_ok = 0
    total = 0
    inv_single = inv_union = 0
    made_up: list[str] = []

    for user, subject, value in GOLD:
        infos = [EX.extract_turn(gen, user, "嗯") for _ in range(ROUNDS)]
        s_hits = sum(1 for i in infos if _hit(i, subject, value))
        # 两两配成并集对（(0,1) (2,3) (4,5)…）
        pairs = [(infos[i], infos[i + 1]) for i in range(0, ROUNDS - 1, 2)]
        u_hits = sum(1 for a, b in pairs if _hit(_union(a, b), subject, value))
        total += 1
        single_ok += (s_hits == ROUNDS)
        union_ok += (u_hits == len(pairs))
        inv_single += sum(_invented(i, user) for i in infos)
        inv_union += sum(_invented(_union(a, b), user) for a, b in pairs)
        flag = "✅" if s_hits == ROUNDS else ("◐" if s_hits else "✗")
        print(f"  {flag} {user}")
        print(f"       单次命中 {s_hits}/{ROUNDS}　并集命中 {u_hits}/{len(pairs)}"
              f"　期望「{subject}」「{value}」")
        if u_hits > s_hits / 2 and s_hits < ROUNDS:
            made_up.append(f"{user}：单次 {s_hits}/{ROUNDS} → 并集 {u_hits}/{len(pairs)}")

    print()
    print("=" * 96)
    print(f"  「每次都对」的条目：单次 {single_ok}/{total}　并集 {union_ok}/{total}")
    print(f"  编造条数：单次 {inv_single}　并集 {inv_union}"
          f"（并集若明显更多，就说明并集在引入脏东西）")
    print()
    if made_up:
        print("  并集救回来的：")
        for m in made_up:
            print(f"    · {m}")
    print()
    print("判读：")
    print("  · 并集命中明显更高、且编造没涨 → **值得做**（代价是抽取耗时翻倍）")
    print("  · 并集没提升或编造涨了 → 不做")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
