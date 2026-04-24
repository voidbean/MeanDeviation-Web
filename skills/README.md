# 阿狼投资技能库（-阿狼- uid=150058）

> 数据来源：NGA帖子 tid=45974302，时间跨度 2026-01-12 ~ 2026-04-22，共 2190 条发言
> 本技能库由 AI 从原始帖子中提取、归纳，供股票分析 Agent 调用

## 文件索引

| 文件 | 内容 |
|------|------|
| `01_market_phase_framework.md` | 行情阶段划分框架（3-1 / 3-2 / 3-3 / 3-4 / 3-5） |
| `02_position_management.md` | 仓位管理策略 |
| `03_entry_exit_signals.md` | 买卖信号与操作触发条件 |
| `04_sector_rotation.md` | 板块轮动与题材选择逻辑 |
| `05_volume_price_analysis.md` | 量价分析方法 |
| `06_intraday_trading.md` | 日内做T策略（正T / 反T / 双跌停战法） |
| `07_risk_management.md` | 风险管理与止损原则 |
| `08_market_participants.md` | 市场参与者行为分析（GJD / 机构 / 游资 / 量化 / 散户） |
| `09_long_term_positions.md` | 长线持仓逻辑与管理 |
| `10_mindset_discipline.md` | 交易心态与操作纪律 |
| `11_stock_type_playbook.md` | 股票类型分类与对应操作手册（趋势底仓 / 题材打野 / 对标映射 / 避险资源 / 超短战法） |
| `agent_prompt_template.md` | Agent 调用模板（系统提示词 + 快速查询模板） |

## 使用说明

每个技能文件均包含：
- **核心原则**：提炼自阿狼原文的核心观点
- **触发条件**：何时应用该技能
- **操作规则**：具体执行步骤
- **原文引用**：关键语录，带时间戳

Agent 调用建议：分析具体股票或大盘时，优先加载 `01`（判断阶段）→ `05`（量价确认）→ `02`（仓位决策）→ `03`（买卖信号）。
