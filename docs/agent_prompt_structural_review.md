# Agent Prompt 结构性评审

本文评审选股、晋升、操盘四个任务 Prompt 的**结构性问题**，即 Prompt 与系统能力之间的错配。

与 `docs/agent_prompt_review.md` 的区别：那份记录的是实现层缺陷（写错、写漏、与执行层常量不同步），
单条改动量在数行内。本文记录的是 Prompt 建立在系统尚不具备的能力之上的问题，无法通过改写措辞解决，
需要数据管道或执行层配合。

**两份文档的修复不应混排。** 结构性问题一与问题三应先于实现层修复处理：在无跨轮记忆、无反馈指标的前提下
继续增补条文，只会让注入体积继续膨胀，且无从判断增补是否有效。

## 问题一 系统无跨轮记忆，Prompt 假设它有

### 现象

`agent_trading_policy.md:49-51` 要求每轮复核持仓的「最初/最近买入理由」；
`agent_stock_entry_policy.md:36-37` 要求买入资格结论写明「可验证的失效条件」；
四个 Prompt 的 `report.focus` 均定义为「下一交易日/下一轮需重点验证的变化」。

这些要求都预设模型能读到自己此前写过的内容。

### 事实

`data/trading_decision_repository.py:100` 的 `_compact_stock` 是显式字段白名单。经核查，其中：

- **能传递**：`screen.extra.ai_selection`（第 115、185 行）保留了每日选股当时给出的 `reason` / `risk` /
  `confidence` / `entry_route`；`lifecycle`、`promotion`、`logic_change` 同样在白名单内。
- **不能传递**：操盘任务自身历史决策的 `reason`、`risk`、`report.focus`，以及任何形式的失效条件。
  持仓对象（`position` / `live_position`）只有价格、数量、盈亏、`available_to_sell` 等交易状态字段，
  不含开仓时的判断依据。

上一轮写下的 `reason` 确实落库了（`orders.reason`、`live_trade_intents.reason`、
`agent_decision_submissions.decision`），但 `_snapshot`（第 60 行）与 `_compact_stock` 都不读取它们，
`get_trading_overview`（第 301 行）因此不携带。

`get_recent_trading_activity`（第 430 行）会返回 `orders` / `live_trade_intents` 的成交流水，其中带
`reason` 字符串，但：一是它是按账户取最近 N 条（上限 20）的流水，不按持仓组织，不保证覆盖当前每一只持仓；
二是 `agent_trading_policy.md:70-72` 已明确把它定位为「只作为组合风险背景」，而非决策依据。

### 影响

选股理由能跨到操盘，但操盘的判断无法跨轮延续。按 crontab，模拟盘每日 8 轮、实盘每日 8 轮，
实际形态是同一账户内 16 次互不相识的独立判断：

- 09:30 写下「失效条件：跌破平台且量能萎缩」；
- 10:00 读不到该结论，只能依据当前价量重新构造理由；
- 而 `agent_trading_policy.md:41-42` 同时要求「不得因历史上曾经强势而保留已经失效的股票」——
  在读不到此前判断的前提下，模型没有判定「已经失效」的依据。

最坏的后果不是模型遗忘，而是**为满足格式而编造**：Prompt 要求复核「最初买入理由」，模型只能就地生成一个
看似合理的理由填入字段，且该字段不会被任何校验发现。

### 方向

二选一，不应维持现状：

- 补齐回传：在 `_compact_stock` 白名单中增加该持仓最近一次操盘决策的 `reason` / `risk` / 失效条件，
  由 `_snapshot` 从 `orders` 或 `agent_decision_submissions` 按 code 关联取出；
- 或删除 Prompt 中所有依赖跨轮记忆的要求，明确本系统按单轮独立判断运行。

## 问题二 推理类约束不可执行

### 事实

按执行层是否校验，Prompt 中的约束可分为两类：

| 类型 | 举例 | 校验情况 |
| --- | --- | --- |
| 结构性 | `reviewed_codes` 全覆盖、逐股一行、T+1 可卖、`target_amount>0`、`entry_route` 合法、持仓上限 | 硬校验，违反即整轮失败 |
| 推理性 | 「多类独立证据同向」「不得重复计数」「放量滞涨优先否决」「分别评估强度、可靠性与持续性」 | 无任何校验 |

`reason` 字段在 `scripts/execute_trading_cycle.py:76` 仅被截断至 180 字符，不存在与证据的一致性校验。
`confidence` 经全仓检索，只在 `scripts/execute_trade_signal.py:166` 被拼接进订单备注字符串，
不参与仓位、风控或任何执行分支。

### 影响

推理类禁令占全部禁令的多数，而遵守与假装遵守的代价相同：写一句「多类证据同向确认」即可通过全部校验。
系统无法区分真正完成交叉验证的决策与仅生成合规话术的决策。

`agent_stock_entry_policy.md:34-35` 的「不得只提高 `confidence` 而始终不改变实际动作」尤其值得注意：
由于 `confidence` 在系统中本无行为影响，该禁令自身不构成任何实际约束。

### 方向

承认这类约束是引导而非规则，据此调整预期，并把有限的强制力集中到可校验项上；
若确需约束推理质量，则需要引入可机检的结构化证据字段（例如要求 `reason` 引用具体证据键名），
而非继续增加自然语言禁令。

## 问题三 缺少反馈回路，Prompt 不可证伪

### 事实

`agent_decision_submissions`（`data/store/schema.py:751-768`）完整保存了 `decision`、`result`、
`model`、`as_of`、`stage`。但经检索全部读取方：

- `data/agent_submissions.py`：仅做幂等认领与状态更新；
- `data/selection_report.py:24-30`：仅取最近一条 `ready` 记录用于渲染页面报告。

没有任何代码将决策与其后续收益关联。该账本是只写不评的。

`close-review`（15:30，`scripts/monitor_close.py:248`）产出的是账户净值、当日涨跌与持仓盈亏，
属于**账户结果**，不是**决策质量**。系统当前无法回答：`strong` 置信度的决策命中率是否高于 `weak`；
`early_start` 与 `strong_continuation` 哪条路线更有效；哪些禁令实际改变了结果。

### 影响

Prompt 不可证伪。增删任一条文都缺乏指标判断其效果，迭代只能依赖直觉。

`tests/test_trading_policy_balance.py` 的 15 个测试（已运行，全部通过）断言的是**措辞是否存在**
（如 `assertIn("盈利仓位以继续持有为默认选择", policy)`），不是措辞是否有效。全绿只证明关键语句未被误删，
不构成策略或 Prompt 有效性的证据，需避免由此产生的安全感错觉。

叠加实现层评审中记录的「Prompt 无版本号」：既无法评估当前版本效果，也无法回溯某个历史决策由哪一版产生。

### 方向

建立决策与结果的关联表，让 `close-review` 除净值外产出决策质量指标（按 `confidence`、`entry_route`、
`stage` 分组的命中率与盈亏分布），并在 `decision.json` 中记录 Prompt 版本号。这是其余所有条文获得
迭代依据的前提。

## 问题四 职责边界形式分离、字段级耦合未披露

### 事实

架构文档声明四个任务严格隔离，但 `buy_eligible` 与 `setup_stage` 两个字段跨越了该边界：

- `scripts/execute_stock_selection.py` 在发布选股结果时写入 `buy_eligible=True`、
  `setup_stage="actionable"`；晋升任务通过后写入同样字段；
- 操盘的新开仓硬门槛正是校验这两个字段（`scripts/execute_trading_cycle.py:194`）。

即选股与晋升实质持有操盘的开仓否决权：标记为不可买时，操盘无论证据多强都无法开仓。

该设计本身可以是合理的，但存在两个问题：一是这一权力关系仅存在于代码中，四个 Prompt 均未向模型明示；
二是与实现层评审的 P0-1 叠加后，形成「使用简化入场标准的环节，控制着使用完整标准环节的开仓权限」。

另外，模拟盘与实盘共享同一候选池与同一份 `agent_stock_entry_policy.md`，但各自独立决策且互不可见
（两份 Prompt 均明令不得讨论或依赖对方账户事实）。两个账户对同一只股票给出相反结论时，系统不视为异常。
而「同标准、同证据、相反结论」恰是暴露标准内部歧义的高价值信号，当前架构主动丢弃了它。

### 方向

在 Prompt 中明示 `buy_eligible` / `setup_stage` 的来源与否决语义，使模型理解自身权限边界；
并考虑离线（不影响账户隔离）比对两账户对同股结论的分歧，作为 Prompt 歧义的发现手段。

## 与实现层评审的关系

| 维度 | `agent_prompt_review.md` | 本文 |
| --- | --- | --- |
| 问题性质 | Prompt 写错、写漏、与常量不同步 | Prompt 依赖系统不具备的能力 |
| 典型修复 | 改写数行文本、同步一个数字 | 改数据管道、加反馈链路 |
| 是否可独立完成 | 是 | 否，需执行层与数据层配合 |
| 建议时序 | 结构性问题一、三处理后再批量修 | 优先 |

## 建议处理顺序

1. **问题一**：在持仓证据中回传上一轮操盘决策的 `reason` / `risk` / 失效条件，
   改动集中在 `_snapshot` 与 `_compact_stock`；或删除相关 Prompt 要求。
2. **问题三**：建立决策与收益的关联表，`close-review` 产出决策质量指标；`decision.json` 记录 Prompt 版本。
3. **问题四**：在 Prompt 中明示字段级否决语义。
4. **问题二**：重新评估推理类条文的定位，收缩至可校验项，或引入结构化证据引用。

前两条完成后，`docs/agent_prompt_review.md` 中的实现层修复才具备效果验证条件。
