#!/usr/bin/env python
"""全量回归：把所有测试串起来跑一遍，输出一张汇总表（项目改进v1 四个模块）。

用法：
    python tests/run_all.py                # 全部（含需要模型/服务的长测）
    python tests/run_all.py --quick        # 跳过最慢的几个（阶段 2 长测、阶段 3、5）
    python tests/run_all.py --only 8 10    # 只跑指定编号
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
PROJECT = ROOT.parent
PY = sys.executable

# (编号, 脚本, 说明, 是否慢)
SUITE = [
    ("冒烟", "smoke_stage2.py", "记忆/检索/反馈基础", False),
    ("BM25", "bm25_test.py", "BM25 词法分与融合（检索降级路径）", False),
    ("自拒判据", "selfrefuse_calibration.py",
     "self_refused 判定的正反例（拒绝/非拒绝都不能误判）", False),
    ("工具协议", "tool_protocol_test.py", "中文定界符工具调用协议（解析/转义/安全取向）", False),
    ("工具循环", "tool_loop_test.py", "工具调用循环（回填/防打转/收尾，用桩函数不调模型）", False),
    ("工具接线", "tool_loop_wiring_test.py", "模型自选工具接入 app.py（路由/决策/执行/回填全链路）", False),
    # 自动拉起嵌入服务：**不连真模型、不起服务**，只验位置约束与降级取向。
    # 挂在这里是为了钉住「构造 Companion 不能拉起服务」这条硬要求。
    ("嵌入自动拉起", "embed_autostart_test.py",
     "启动时自动拉起 8081（位置约束/开关/不抢占，不起真服务）", False),
    # 一跳扩展：多跳联想靠关系记忆（别名→名字）补上。
    # 只用词法（index=None），不需要嵌入服务。
    ("一跳扩展", "hop_expansion_test.py",
     "多跳联想的一跳扩展（有锚点才跳/跳数衰减/不凭空多带）", False),
    # 索引回填：库里有、索引里没有的要能补上（用桩 embed，不连服务）
    ("索引回填", "reindex_test.py",
     "索引回填（找缺口/dry-run/幂等/服务不可用即返回/原子落盘）", False),
    # 关系抽取：双向绑定 + 编造名字的接地校验（纯函数，不调模型）
    ("关系抽取", "relation_grounding_test.py",
     "关系统一落成「名字（关系）」+ 编造名字降级（不调模型）", False),
    # 名字识别口径统一（原来四处各写一套，会造出「工业软」这种假实体）
    ("名字口径", "terms_test.py",
     "名字识别统一口径（长名不截断/无标点不过抓/机构常用字不误裁）", False),
    # 抽取器与聊天分离：主路径 / 回落 / 接线位置（用桩，不连模型）
    ("抽取器分离", "extractor_test.py",
     "抽取器与聊天解耦（主路径/逐次回落/接线位置，不连模型）", False),
    # 第③层逐字原话注入（此前是死通道；最易错的是 recent 最新在前导致永远为空）
    ("原话注入", "verbatim_inject_test.py",
     "逐字原话注入（只引短窗口外/上限/预算保底/接线防退回死代码）", False),
    # 冲突合并：模型合并 + 硬护栏（数字/实词接地）+ 失败什么都不改（用桩，不连模型）
    ("冲突合并", "conflict_merge_test.py",
     "冲突合并（护栏拦住编造/失败不丢数据/旧条可追溯/救回被藏条目）", False),
    # 别名表：简称归并 + 护栏 + 查询扩展 + BM25 缓存键含 known（不连模型）
    ("别名归并", "alias_test.py",
     "别名归并（简称学习/歧义不覆盖/查询扩展/缓存失效/人工否掉不复活）", False),
    # 主体校验：抽取不能把别人的事记成用户自己的（比抽不到更糟）
    ("主体校验", "subject_guard_test.py",
     "主体校验（第三方的事不许记成用户的；误杀就是丢记忆，宁可少拦）", False),
    # 状态句被误归成关系/宾语被吞 → 从原话还原成事实（不连模型）
    ("状态句还原", "state_fact_test.py",
     "状态句还原（状态词当关系／宾语被吞；从原话补全，角色要放行）", False),
    # 记忆组装层：实体归一/图遍历/置信度分层/冲突标注/自然语言化（不连模型）
    ("记忆组装", "assemble_test.py",
     "记忆组装层（接地强度分层/别名归一/图遍历/不塞JSON/预算不打折）", False),
    # 记忆 SFT 训练样本的数据质量闸（不连模型）
    # 训练数据错了比没数据更糟——会把错误固化进模型（实测真踩过"主语串人"）
    ("训练样本", "memory_sft_data_test.py",
     "记忆SFT样本（带块/不念提示词/主语不串/校验字段齐全/八类覆盖）", False),
    # 不确定 → 提问 → 用户回答 → 关联回那条记忆（闭环）
    # 重点钉：判断从紧（顺从偏差防护）、**同轮不自问自答**、确认升权/否认不注入
    ("回引确认", "clarify_test.py",
     "回引确认（判断从紧/同轮不自问自答/确认升权/否认不注入/不叠问/过期）", False),
    # **确定性组装指标**：不测模型，测"该进块的东西进没进、排第几"（快、可复现）
    ("组装指标", "analysis/assemble_metric.py",
     "组装指标（召回/纯净/首项位置/预算——程序侧改动的回归线）", False),
    ("8", "stage8_memory_life_test.py", "重要度生命周期", False),
    ("9", "stage9_service_test.py", "服务管理（真启停）", True),
    ("10", "stage10_ai_profile_test.py", "AI 档案与动态性格", False),
    ("11", "stage11_tools_cloud_test.py", "工具接入与云端边界", False),
    ("12", "stage12_mcp_test.py", "MCP 客户端", True),
    ("13", "stage13_sessions_migration_test.py", "多会话与迁移备份", False),
    ("14", "stage14_memory_quality_test.py", "情绪标签与去重合并", False),
    ("15", "stage15_conflict_test.py", "冲突处理", False),
    ("16", "stage16_import_test.py", "记忆导入", False),
    ("17", "stage17_links_test.py", "记忆关联", False),
    ("18", "stage18_maintenance_test.py", "长期维护", False),
    ("19", "stage19_theme_test.py", "抽象聚类", False),
    ("20", "stage20_linkage_test.py", "与价值观/技能/人格联动", False),
    ("21", "stage21_onboarding_test.py", "冷启动策略", False),
    ("导出", "training_export_test.py", "训练集导出", False),
    ("接口", "test_pet_server.py", "对外接口（HTTP，用假 companion，不需要模型）", False),
    ("TTS", "stage7_tts_test.py", "语音合成接口", True),
    # 主窗口布局：每个功能按钮必须完整落在窗口内（几何验证，不靠截图）
    # 界面接线：self.xxx 被引用就必须有定义（防整段替换顺手删掉方法）
    ("界面接线", "ui_wiring_test.py",
     "界面接线（self.xxx 都有定义/11 个关键方法在）", False),
    ("布局", "layout_test.py",
     "主窗口布局（17 个按钮全在窗口内/输入框可见/形象图已加载）", False),
    ("GUI", "gui_smoke_test.py", "桌面窗口", True),
    # 编造兜底：没有记忆依据时剪掉「你上次也…」（不连模型）
    # 实测 6/24=25% → 补 CLAIM_MARKS+重生成仍 21% → 确定性剪句 0%
    # 两段式补充的判定工具与配置默认（不连模型）
    # 钉：逐句判重复（整段比会被稀释）／补充剪光返回空／**默认关**
    # 时间技能：工具函数 + 路由触发（不连模型）
    # ⚠ 重点钉「今天好累」不许被时间吸走——今天 是极高频词
    # 编属性防线：回复里的数字必须能从证据里找到（不连模型）
    # 修的是「2026 年——你还没满 18 周岁呢」这类**凭空断言用户属性**
    # 五个角色的模型配置 + 判官规则（不连模型）
    # ⚠ 重点钉「**判官只能是 9B/4B，2B 不许**」和「9B 在就不用 4B」
    # 思考模型按需起停（不连模型）
    # ⚠ 重点钉「l1 不许触发」（起要 17 秒，闲聊不能为它等）和「默认 judge_only」
    # 共用模型防呆：同一模型配给多角色时只起一份（不连模型）
    ("角色共用", "shared_role_test.py",
     "共用防呆（按路径判同一文件/共用时端口指向对方/embed 不共用）", False),
    ("思考模型", "think_slot_test.py",
     "思考模型起停（深档才触发/开关一致/写错 mode 退回/只关自己起的）", False),
    # 角色走远程 API + **外发网关不能绕过**（不联网）
    # 固定回复由 config/replies.yaml 维护（改文件即改话术/坏文件不崩）
    ("固定回复", "replies_test.py",
     "固定回复文件（改文件即生效/坏文件回退默认/调用点都接上）", False),
    ("API角色", "api_role_test.py",
     "角色走 API（endpoint 指向 API/Bearer 头/**网关默认拦住**）", False),
    ("模型角色", "models_config_test.py",
     "五角色配置（每角色解析到真文件/判官只能 9B·4B/9B 优先/都没跑就跳过）", False),
    ("编属性", "attribute_guard_test.py",
     "编属性防线（抓到编的数字/**用户说过的不误抓**/「一个人」不算数量）", False),
    ("时间技能", "clock_skill_test.py",
     "时间技能（工具含年月日时分周/时间类命中/**今天好累不许被抢**）", False),
    ("补充判定", "supplement_guard_test.py",
     "两段式补充（逐句判重复/剪光返回空/配置默认关）", False),
    ("剪编造", "claim_guard_test.py",
     "剪掉无凭据的旧事指涉（该剪的剪、不该剪的不动、不误伤『以前』）", False),
    ("4", "stage4_style_test.py", "深度路由与风格", True),
    ("5", "stage5_values_test.py", "价值观养成", True),
    ("3", "stage3_skilltest.py", "技能与边界", True),
    ("2", "stage2_longtest.py --turns 20", "长对话（20 轮）", True),
]


def run_one(script: str, timeout: int = 1200) -> dict:
    """跑一个测试。

    注意：这里**不能用管道捕获子进程输出**（沙箱下会 EPERM），
    所以让子进程把输出重定向到临时文件，跑完再读文件。
    """
    parts = script.split()
    cmd = [PY, str(ROOT / parts[0])] + parts[1:]      # 脚本在 tests/ 下，cwd 是项目根
    started = time.time()
    log_path = ROOT / "_tmp" / "runall"
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / (script.split()[0].replace(".py", "")
                           .replace("/", "_").replace("\\", "_") + ".log")
    # ⚠ 日志名要把路径分隔符换掉（2026-10-16）：子目录脚本（`analysis/xxx.py`）
    #   直接把路径当文件名，会在 `_tmp/runall/` 下找不到子目录 → FileNotFoundError。
    try:
        with open(log_file, "w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.run(cmd, cwd=str(PROJECT), stdout=fh,
                                  stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        out = log_file.read_text(encoding="utf-8", errors="replace")
        tail = [line for line in out.strip().splitlines() if line.strip()]
        summary = ""
        for line in reversed(tail):
            if "通过" in line or "全部通过" in line:
                summary = line.strip()
                break
        summary = summary or (tail[-1].strip()[:60] if tail else "（没有输出）")
        return {"ok": proc.returncode == 0, "summary": summary,
                "seconds": round(time.time() - started, 1), "output": out}
    except subprocess.TimeoutExpired:
        return {"ok": False, "summary": f"超时（>{timeout}s）",
                "seconds": round(time.time() - started, 1), "output": ""}
    except Exception as exc:
        return {"ok": False, "summary": f"起不来：{type(exc).__name__} {exc}"[:60],
                "seconds": round(time.time() - started, 1), "output": ""}


def main() -> int:
    parser = argparse.ArgumentParser(description="全量回归")
    parser.add_argument("--quick", action="store_true", help="跳过慢测")
    parser.add_argument("--only", nargs="*", default=None, help="只跑这些编号/名称")
    args = parser.parse_args()

    picked = []
    for number, script, label, slow in SUITE:
        if args.only and number not in args.only and label not in args.only:
            continue
        if args.quick and slow:
            continue
        picked.append((number, script, label, slow))

    print(f"要跑 {len(picked)} 项；开始时间 {dt.datetime.now():%H:%M:%S}\n")
    results = []
    for number, script, label, _slow in picked:
        sys.stdout.write(f"▶ [{number}] {label} … ")
        sys.stdout.flush()
        got = run_one(script)
        results.append((number, label, got))
        print(f"{'✓' if got['ok'] else '✗'} {got['seconds']}s　{got['summary'][:50]}")

    passed = sum(1 for _, _, got in results if got["ok"])
    print(f"\n=== 汇总：{passed}/{len(results)} 通过 ===")
    for number, label, got in results:
        if not got["ok"]:
            print(f"  ✗ [{number}] {label}：{got['summary'][:80]}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    report = PROJECT / "samples" / f"{stamp}-全量回归.md"
    report.parent.mkdir(exist_ok=True)
    lines = [f"# 全量回归 {dt.datetime.now():%Y-%m-%d %H:%M:%S}", "",
             f"通过 {passed}/{len(results)}", "",
             "| 项 | 说明 | 结果 | 耗时 |", "|---|---|---|---|"]
    for number, label, got in results:
        lines.append(f"| {number} | {label} | {'✓' if got['ok'] else '✗'} "
                     f"{got['summary'][:40]} | {got['seconds']}s |")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告已保存：{report}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
