# 训练集导出报告

- 导出时间：2026-10-06 03:07:22
- 来源：情景记忆 0 轮、偏好对文件 0 条、情境数据 0 条

## 产出

- `sft_messages.jsonl`：0 条（含 /good 优质样本）
- `sft_alpaca.jsonl`：0 条
- `dpo.jsonl`：0 对（只有 /fix 能组成真正的对）
- `safety.jsonl`：0 条（拒绝类，默认不进 SFT）

## 质检

- 过滤：太短 0、太长 0、重复 0
- DPO 里凑不成对的偏好记录：0 条（/good /bad 只有单边，想要偏好对请多用 /fix）
- 平均长度：用户 0 字、未央 0 字
- 档位分布：无
- 技能分布：无

## 怎么用

LLaMA-Factory 的 SFT：`dataset_info.json` 里把 `sft_messages.jsonl` 的 `formatting` 设为 `sharegpt`（messages 字段）；DPO 直接用 `dpo.jsonl`。
微调完记得用 `tests/` 下的阶段 2-5 测试对着新模型重跑一遍，对比记住率、风格一致性和安全违规率。
