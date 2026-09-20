# Agent 决策架构

## 原则

Prompt 只负责指导模型如何理解证据和做判断；脚本负责事实范围、状态、动态约束、提交结构和确定性校验。
任何会随账户、运行时或执行器变化的值都不能在 Prompt 中维护第二份副本。

## 三层边界

1. **数据与执行层**：生成不可变 `as_of` 快照，维护账户隔离、T+1、禁买范围、容量、字段限制和动作执行。
2. **决策契约层**：`data/agent_decision_contracts.py` 是动作、值域、批量、字段和提交结构的单一来源；各 overview
   通过 `decision_contract` 将当前契约返回给模型，校验器直接导入同一组常量。
3. **策略 Prompt 层**：只包含任务目标、必读数据、字段语义、证据权衡和买入/持有/退出原则，不保存 JSON 大模板、
   运行时历史或可变常量。

## Prompt 装配

`scripts/run_stock_agent.py` 的 `TASK_SPECS` 声明每类任务依赖的共享策略和 submit 工具：

- selection、promotion：共享个股买入资格标准；
- trading-simulated、trading-live：共享个股买入资格标准 + 账户操盘策略；
- 四类任务再追加各自的短 Prompt 和统一运行时提交说明。

不得在运行入口新增按任务散落的 Prompt 拼接条件。完整装配结果计算 SHA-256 内容版本，并写入 run artifact 和
`agent_decision_submissions.prompt_version`。

## 决策连续性

`stock_evidence.decision_context` 由脚本回传真实的原始/最近入场理由和上一轮逐股决策；`previous_round` 回传上一轮
全局关注与风险。历史缺失时模型按当前证据独立判断，不能重构或编造历史理由。

## 修改规则

- 改执行动作、字段或限制：先改 `agent_decision_contracts.py` 和确定性校验，Prompt 通常无需修改。
- 改数据含义：修改数据生产/工具契约，并在共享策略中只保留稳定的解释原则。
- 改交易判断：修改共享策略或任务 Prompt，不在执行器中复制自然语言策略。
- 新增任务：在 `TASK_SPECS` 声明依赖，提供 overview `decision_contract`，并增加结构与行为测试。
- 不用追加例外句修补冲突；发生冲突时先消除重复真相来源。

测试只验证声明结构、机器契约、校验器和执行行为。不得通过 `assertIn`、正则等方式断言 Prompt 中存在或不存在
某段文案；这类测试只能锁定措辞，不能证明决策正确性，也会阻碍正常的 Prompt 精简与迭代。
