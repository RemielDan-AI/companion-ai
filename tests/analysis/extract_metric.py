"""**抽取质量指标**：2B 抽取器现在到底还有哪些问题（固定金标准 + 多次跑逐条定位）。

## 为什么要单独量这一条

抽取在最上游：它抽错/抽丢，后面**全白搭**——组装层再干净，
库里的东西本身就是错的。此前都是零散探针（主体保真 6/6、状态句 7/8…），
没有一个**统一的、可重复的**体检。

## 量六个维度（每条都对应一个真实踩过的坑）

    主体保真   说的那个人的事，别记成用户自己的（实测「小雨…」→「用户…」）
    值保真     关键宾语别丢（实测「对花生过敏」→ 只留「过敏」）
    字段正确   relations 必须是「名字（关系）」；状态词（过敏/怕狗）不该当关系
    不编造     不许出现原话里没有的内容（实测「在广州工作」）
    不丢失     关键信息要留在**某个**字段里（facts/relations/preferences 都行）
    该空就空   空轮/纯提问，不该硬凑出东西（实测「嗯，还行吧」抽出 3 条）

## 判据为什么这样定

抽取是**随机**的（temp 0.2），单次不能下结论 → 每条跑 REPEAT 次，
报"几次里对几次"。**要区分"稳定错"和"偶尔错"**：
稳定错是模型能力问题，偶尔错是抖动。

用法：
    python tests/model_server.py run --model Qwen3.5-2B --ngl 99 \
        --script tests/analysis/extract_metric.py
    （跑的是**生产抽取器**；换 --model 可以横向比别的模型）
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from cognition import extract as EX                    # noqa: E402
from engine.generate import Generator                  # noqa: E402

REPEAT = max(1, int(os.environ.get("EXTRACT_REPEAT", "3")))

# 状态词当关系 = 分类错（值会被吞）
_STATE = ("过敏", "喜欢", "讨厌", "住在", "上班", "上学", "读书", "工作", "怕")
_PAIR = re.compile(r"(.{2,12})（(.{1,8})）")


def _is_state(w: str) -> bool:
    return any(s in w for s in _STATE)


# (用户原话, 主体, 值, 说明)
GOLD = [
    ("我妹妹叫林小雨", "林小雨", "", "显式命名"),
    ("林小雨对花生过敏", "林小雨", "花生", "状态句：宾语最容易丢"),
    ("我妹妹对海鲜过敏", "妹妹", "海鲜", "状态句（有「我」）"),
    ("我妹妹不吃香菜", "妹妹", "香菜", "否定式"),
    ("我住在南京", "南京", "", "纯第一人称 + 地点"),
    ("我已经搬到成都了", "成都", "", "状态变更"),
    ("我养了只猫叫团子", "团子", "", "宠物命名"),
    ("团子三岁了", "团子", "三岁", "宠物属性"),
    ("我同事陈思远做后端", "陈思远", "后端", "同事 + 职业"),
    ("小花是我妹妹的同学", "小花", "", "第三方关系句"),
    ("我妹妹在广州大学上学", "广州大学", "广州大学", "地点 + 机构"),
    ("我妈在杭州住院了", "杭州", "住院", "家人 + 地点 + 事件"),
]

# 这些**不该抽出实质内容**（空轮 / 纯提问）
QUIET = [
    ("嗯，还行吧", "空轮，没什么可抽"),
    ("你觉得我该不该转学", "只是提问，没有事实"),
]


def _blob(info: dict) -> str:
    parts = []
    for k in ("facts", "preferences", "relations"):
        parts += [str(x) for x in (info.get(k) or [])]
    return " ".join(parts)


def _relations_well_formed(info: dict) -> tuple[bool, str]:
    """relations 必须是「名字（关系）」，且关系词不能是状态词。"""
    for r in (info.get("relations") or []):
        m = _PAIR.search(str(r))
        if not m:
            return False, f"不是「名字（关系）」：{r}"
        if _is_state(m.group(2)):
            return False, f"**状态词当关系**：{r}"
    return True, ""


def _invented(info: dict, user_text: str) -> str:
    """有没有原话里根本没有的内容（接地为 0 = 纯编）。"""
    for k in ("facts", "preferences"):
        for item in (info.get(k) or []):
            body = EX._canonical(k[:-1], str(item))
            for pfx in ("对方提到的人：", "对方", "用户"):
                if body.startswith(pfx):
                    body = body[len(pfx):]
                    break
            if EX._grounded(body, user_text):
                continue
            return f"[{k}] {item}"
    return ""


def main() -> int:
    url = os.environ.get("COMPANION_API_URL") or "http://127.0.0.1:8082/v1/chat/completions"
    tag = os.environ.get("EXTRACT_MODEL_TAG", "")
    gen = Generator(api_url=url)
    print(f"抽取质量体检　模型：{tag or url}　每句跑 {REPEAT} 次")
    print("=" * 100)

    dim = {k: [0, 0] for k in ("主体", "值", "字段", "不编造", "不丢失", "该空就空")}
    problems: list[str] = []

    for user, subject, value, why in GOLD:
        good = {"主体": 0, "值": 0, "字段": 0, "不编造": 0, "不丢失": 0}
        samples = []
        for _ in range(REPEAT):
            info = EX.extract_turn(gen, user, "嗯")
            blob = _blob(info)
            samples.append(info)
            good["主体"] += bool(subject and subject in blob)
            good["值"] += (bool(value and value in blob) if value else 1)
            ok, _r = _relations_well_formed(info)
            good["字段"] += ok
            good["不编造"] += (not _invented(info, user))
            good["不丢失"] += bool(blob.strip())
        n = REPEAT
        flags = []
        for k, v in good.items():
            dim[k][1] += 1
            dim[k][0] += (v == n)
            if v < n:
                flags.append(f"{k} {v}/{n}")
        mark = "✅" if not flags else ("◐" if len(flags) < 3 else "✗")
        print(f"\n{mark} {why}　用户：{user}")
        print(f"     期望主体「{subject}」值「{value}」　→ {flags or '全对'}")
        if flags:
            for info in samples[:2]:
                bad, r = _relations_well_formed(info)
                print(f"     facts={[str(f)[:26] for f in (info.get('facts') or [])]}")
                print(f"     rels ={info.get('relations')}"
                      + (f"　⚠ {r}" if not bad else ""))
                inv = _invented(info, user)
                if inv:
                    print(f"     ⚠ 编造：{inv[:40]}")
            problems.append(f"{why}（{user}）：{flags}")

    print()
    print("=" * 100)
    print("该空就空（空轮/提问不该硬凑）")
    print("=" * 100)
    for user, why in QUIET:
        empty = 0
        for _ in range(REPEAT):
            info = EX.extract_turn(gen, user, "嗯")
            empty += (not _blob(info).strip())
        dim["该空就空"][1] += 1
        dim["该空就空"][0] += (empty == REPEAT)
        mark = "✅" if empty == REPEAT else ("◐" if empty else "✗")
        print(f"  {mark} {why}　用户：{user}　抽空 {empty}/{REPEAT}")
        if empty < REPEAT:
            info = EX.extract_turn(gen, user, "嗯")
            print(f"     抽出：{[str(x)[:24] for x in (info.get('facts') or [])]}"
                  f" {info.get('relations')}")
            problems.append(f"{why}（{user}）：抽出 {empty}/{REPEAT} 次空")

    print()
    print("=" * 100)
    print("② **落库后**的结果（经过 remember_turn 的规则修补）——这才是系统真正用的")
    print("=" * 100)
    print("（抽取器原始输出有毛病不可怕，怕的是**兜不住、漏进库**）")
    from cognition.extract import remember_turn
    from cognition.memory import Memory
    import tempfile

    leak: list[str] = []
    stored_ok = 0
    for user, subject, value, why in GOLD:
        d = pathlib.Path(tempfile.mkdtemp())
        mem = Memory(str(d / "m.db"))
        try:
            remember_turn(mem, gen, user, "嗯", session="s", turn=1, index=None)
        except Exception as exc:
            print(f"  ✗ {why}：入库失败 {type(exc).__name__}: {exc}")
            continue
        stored = [str(i["content"]) for i in mem.all_semantic(status="active")]
        blob = " ".join(stored)
        marks = []
        if subject and subject not in blob:
            marks.append(f"**主体丢了**（缺「{subject}」）")
        if value and value not in blob:
            marks.append(f"**值丢了**（缺「{value}」）")
        if not stored:
            marks.append("**整句没进库**")
        if marks:
            leak.append(f"{why}（{user}）：{'、'.join(marks)}")
            print(f"  ✗ {why}　{user}")
            print(f"     入库 = {[s[:26] for s in stored]}")
        else:
            stored_ok += 1
            print(f"  ✅ {why}　{user}")

    print()
    print("=" * 100)
    print(f"汇总（每条按「{REPEAT} 次全对」才算过）")
    print("=" * 100)
    for k, (got, cap) in dim.items():
        bar = "█" * int(10 * got / cap) + "·" * (10 - int(10 * got / cap))
        print(f"  {k:<8} {bar} {got}/{cap}")
    n_gold = len(GOLD)
    print(f"  {'落库后':<8} {'█' * int(10 * stored_ok / n_gold)}"
          f"{'·' * (10 - int(10 * stored_ok / n_gold))} {stored_ok}/{n_gold}")
    print()
    if problems:
        print("**抽取器原始输出**仍有问题的：")
        for p in problems:
            print(f"  · {p}")
    print()
    if leak:
        print(f"⚠⚠ **漏进库的**（规则没兜住，{len(leak)} 条）——这些才是要修的：")
        for p in leak:
            print(f"  · {p}")
    else:
        print("✅ **没有漏进库的**：抽取器的毛病都被规则兜住了")
    print()
    print("读法：")
    print("  · 抽取器分低、落库分高 → 规则在兜，**优先级低**")
    print("  · **落库分低** → 真的进了库，组装/生成全被污染，**必须修**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

