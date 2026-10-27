# 技能文件格式说明（不参与加载，供查阅）
#
# id            技能编号（S=核心 E=扩展 F=禁止）
# name          技能名称
# kind          core / extension / forbidden
# enabled       是否启用（扩展技能占位时 false）
# description   一句话说明
# triggers      keywords 触发词、patterns 正则、fallback 是否为兜底技能
# input         输入说明（free_text / 结构化字段）
# executor      执行器：type=llm|memory_recall|reminder|none
#               llm 可指定 level（档位）与 prompt（本技能专属提示，内联，保证自包含）
# limits        限制：max_tokens、top_k 等
# refusal       拒绝话术：kind=soft|hard|transfer|safety，text 为本技能专属话术
#
# 路由顺序：forbidden（禁止）→ extension（未实现的扩展）→ core（核心）→ fallback
# 新增技能 = 往目录里放一个 yaml，不需要改任何代码。
