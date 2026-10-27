"""技能路由：决定一句话交给哪个技能，还是该拒绝。

顺序（技能白名单，默认拒绝）：
  1. forbidden  禁止技能命中 → 直接拒绝，不生成
  2. extension  未实现的扩展技能命中 → 软拒绝 + 替代路径
  3. core       核心技能命中 → 按技能的执行器处理
  4. 任务型请求（"帮我查/写/算…"）但没技能接 → 默认拒绝
  5. fallback   闲聊兜底

每个技能是一个自包含 YAML（见 skills/README.md），新增技能只需放文件，不用改这里。
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL_DIRS = {
    "forbidden": ROOT / "skills" / "forbidden",
    "extension": ROOT / "skills" / "extension",
    "core": ROOT / "skills" / "core",
}

# 明显是在"要一个能力"的说法：没有技能能接时应当拒绝，而不是当成闲聊
CAPABILITY_HINTS = [
    "帮我查", "帮我搜", "帮我写", "帮我算", "帮我翻译", "帮我画", "帮我生成",
    "搜索一下", "查一下", "查查", "计算一下", "算一下", "写个代码", "写段代码",
    "写代码", "编程", "爬虫", "写一篇", "写个方案", "帮我做个",
]


@dataclasses.dataclass
class Decision:
    kind: str                     # skill / refuse
    action: str                   # llm / memory_recall / reminder / refuse
    skill_id: str = ""
    skill_name: str = ""
    executor: dict = dataclasses.field(default_factory=dict)
    limits: dict = dataclasses.field(default_factory=dict)
    refusal_kind: str = ""
    refusal_text: str = ""
    reason: str = ""

    def brief(self) -> str:
        if self.kind == "refuse":
            return f"拒绝（{self.refusal_kind or 'soft'}）· {self.skill_name or '无技能'}"
        return f"{self.skill_id} {self.skill_name} · {self.action}"


class SkillRouter:
    def __init__(self, dirs: dict | None = None):
        self.dirs = dirs or SKILL_DIRS
        self.skills: list[dict] = []
        self.load()

    # ---------------- 加载 ----------------
    def load(self) -> list[dict]:
        self.skills = []
        for kind, directory in self.dirs.items():
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.yaml")):
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except Exception as exc:
                    print(f"[技能加载失败] {path.name}: {exc}")
                    continue
                data.setdefault("kind", kind)
                data["_path"] = str(path)
                self.skills.append(data)
        return self.skills

    def by_kind(self, kind: str) -> list[dict]:
        return [s for s in self.skills if s.get("kind") == kind and s.get("enabled", True)]

    def fallback(self) -> dict | None:
        for skill in self.by_kind("core"):
            if (skill.get("triggers") or {}).get("fallback"):
                return skill
        return None

    # ---------------- 匹配 ----------------
    def match_score(self, skill: dict, text: str) -> int:
        triggers = skill.get("triggers") or {}
        score = 0
        for keyword in triggers.get("keywords") or []:
            if keyword and keyword in text:
                score += 2
        for pattern in triggers.get("patterns") or []:
            try:
                if re.search(pattern, text):
                    score += 3
            except re.error:
                continue
        return score

    def route(self, text: str) -> Decision:
        text = (text or "").strip()

        # 1) 禁止技能：安全类优先
        hit_forbidden = []
        for skill in self.by_kind("forbidden"):
            score = self.match_score(skill, text)
            if score:
                hit_forbidden.append((score, skill))
        if hit_forbidden:
            hit_forbidden.sort(
                key=lambda pair: ((pair[1].get("refusal") or {}).get("kind") == "safety",
                                  pair[0]),
                reverse=True,
            )
            skill = hit_forbidden[0][1]
            refusal = skill.get("refusal") or {}
            return Decision(
                kind="refuse", action="refuse",
                skill_id=skill.get("id", ""), skill_name=skill.get("name", ""),
                refusal_kind=refusal.get("kind", "hard"), refusal_text=refusal.get("text", ""),
                reason=f"命中禁止技能（{skill.get('name')}）",
            )

        # 2) 扩展技能：enabled=false 表示"占位未实现"，命中也要给诚实的软拒绝
        hit_extension = []
        for skill in self.skills:
            if skill.get("kind") != "extension":
                continue
            score = self.match_score(skill, text)
            if score:
                hit_extension.append((score, skill))
        if hit_extension:
            hit_extension.sort(key=lambda pair: -pair[0])
            skill = hit_extension[0][1]
            refusal = skill.get("refusal") or {}
            executor = skill.get("executor") or {}
            if not skill.get("enabled", True):
                return Decision(
                    kind="refuse", action="refuse",
                    skill_id=skill.get("id", ""), skill_name=skill.get("name", ""),
                    refusal_kind=refusal.get("kind", "soft"),
                    refusal_text=refusal.get("text", ""),
                    reason=f"扩展技能未实现（{skill.get('name')}）",
                )
            return Decision(
                kind="skill", action=executor.get("type", "llm"),
                skill_id=skill.get("id", ""), skill_name=skill.get("name", ""),
                executor=executor, limits=skill.get("limits") or {},
                reason=f"命中扩展技能（{skill.get('name')}）",
            )

        # 3) 核心技能
        hit_core = []
        for skill in self.by_kind("core"):
            if (skill.get("triggers") or {}).get("fallback"):
                continue
            score = self.match_score(skill, text)
            if score:
                hit_core.append((score, skill))
        if hit_core:
            hit_core.sort(key=lambda pair: -pair[0])
            skill = hit_core[0][1]
            executor = skill.get("executor") or {}
            return Decision(
                kind="skill", action=executor.get("type", "llm"),
                skill_id=skill.get("id", ""), skill_name=skill.get("name", ""),
                executor=executor, limits=skill.get("limits") or {},
                reason=f"命中核心技能（{skill.get('name')}）",
            )

        # 4) 任务型请求但没有技能能接 → 默认拒绝
        for hint in CAPABILITY_HINTS:
            if hint in text:
                return Decision(kind="refuse", action="refuse",
                                refusal_kind="soft", reason=f"任务型请求无对应技能（{hint}）")

        # 5) 闲聊兜底
        skill = self.fallback()
        if skill:
            return Decision(kind="skill", action="llm",
                            skill_id=skill.get("id", ""), skill_name=skill.get("name", ""),
                            executor=skill.get("executor") or {},
                            limits=skill.get("limits") or {},
                            reason="闲聊兜底")
        return Decision(kind="refuse", action="refuse", refusal_kind="soft",
                        reason="没有兜底技能")

    # ---------------- 展示 ----------------
    def table(self) -> str:
        lines = []
        label = {"core": "核心", "extension": "扩展", "forbidden": "禁止"}
        for kind in ("core", "extension", "forbidden"):
            skills = [s for s in self.skills if s.get("kind") == kind]
            if not skills:
                continue
            lines.append(f"【{label[kind]}技能】")
            for skill in skills:
                flag = "" if skill.get("enabled", True) else "（未启用）"
                executor = skill.get("executor") or {}
                lines.append(f"  {skill.get('id','?'):<5}{skill.get('name',''):<9}"
                             f"{executor.get('type',''):<14}{skill.get('description','')}{flag}")
        return "\n".join(lines)
