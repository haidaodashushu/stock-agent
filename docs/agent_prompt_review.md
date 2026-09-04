# Agent Prompt 质量评审

本文只评审选股、晋升与操盘四个任务 Prompt 的**工程质量**，不评价交易策略本身。

评审对象：

| 文件 | 作用 |
| --- | --- |
| `config/agent_stock_selection_prompt.md` | 每日最终选股 |
| `config/agent_candidate_promotion_prompt.md` | 观察股盘中晋升 |
| `config/agent_simulated_trading_prompt.md` | 模拟盘半小时操盘 |
| `config/agent_live_trading_prompt.md` | 实盘半小时操盘 |
| `config/agent_stock_entry_policy.md` | 个股买入资格共享标准 |
| `config/agent_trading_policy.md` | 操盘共享策略 |

装配入口：`scripts/run_stock_agent.py`（当前运行时）、`scripts/hermes_*.sh`（仅回滚保留）。

## 结论

设计思路高于一般水平，已经做到：事实唯一来源、账户隔离、缺失即未知、`reviewed_codes` 全覆盖审计、
市场状态不允许模型改判、双向防御（既约束过度交易也约束假性保守）。`tests/test_trading_policy_balance.py`
用 15 个契约测试锁定关键语句，属于少见的好实践。

问题集中在两类：一个结构性口径漏洞，以及一批 Prompt 与执行层不同步。其中 P0 三条会直接导致口径分裂、
无效工具调用或整轮超时。

## P0-1 选股任务缺少《个股买入资格共享标准》注入

`scripts/run_stock_agent.py:115-118` 只对 `promotion`、`trading-simulated`、`trading-live` 注入
`agent_stock_entry_policy.md`，`selection` 被排除；`scripts/hermes_stock_selection.sh` 同样只传单个
Prompt 文件。

由此产生矛盾：

- `scripts/execute_stock_selection.py:79-81` 强制每只入选股必须带合法 `entry_route`；
- 但 `early_start` / `strong_continuation` 的完整判定标准只写在 `agent_stock_entry_policy.md`；
- 于是 `agent_stock_selection_prompt.md:26-29` 自行简写了第二套路线定义。

这违反 `agent_stock_entry_policy.md` 开篇宣称的“唯一标准”，也违反 `agent_trading_policy.md:4-5`
的“不得另建一套模拟盘或实盘入场标准”。选股结论会直接写入 `buy_eligible=true` 与
`setup_stage=actionable`，而这两个字段正是操盘新开仓的硬门槛（`scripts/execute_trading_cycle.py:194`）。
结果是上游用简化口径授予买入资格、下游用完整口径执行。

修复：`selection` 也注入 entry policy，并删除选股 Prompt 内重复的路线定义。

## P0-2 `candidate_lifecycle` 被写成工具名

`agent_stock_selection_prompt.md:24` 要求“使用 `candidate_lifecycle` 比较首次发现时间、连续观察次数……”。

但 `scripts/stock_selection_mcp.py` 只暴露三个工具：`selection_overview`、`candidate_evidence`、
`submit_stock_selection`。`candidate_lifecycle` 实际是 `candidate_evidence` 返回体中的一个字段
（`data/stock_selection_repository.py:434`）。当前写法会诱导模型调用不存在的工具，浪费轮次。

修复：改为“读取 `candidate_evidence` 返回的 `candidate_lifecycle` 字段”。

## P0-3 “每批不超过 5 只”与真实限制相差 8 倍

| 位置 | 值 |
| --- | --- |
| `agent_stock_selection_prompt.md:9-10` | 每批 ≤ 5 只 |
| `data/stock_selection_repository.py:25` | `MAX_EVIDENCE_CODES = 40` |
| `data/trading_decision_repository.py:17` | `MAX_EVIDENCE_CODES = 50` |
| `engine/screener.py:28` | 候选池 100 只 |

100 只按每批 5 只需要约 20 次工具调用，而 `scripts/hermes_stock_selection.sh` 的模型超时为 360s
（`STOCK_SELECTION_MODEL_TIMEOUT` 默认值）。该数字没有技术依据，却显著抬高超时失败率。

修复：改为 20–40，调用次数降到 3–5 次。

## P1-1 “只返回 JSON”在当前运行时已不成立

四个任务 Prompt 结尾与 `agent_trading_policy.md:82-83` 均要求“只返回 JSON、不要输出分析过程”。
但当前 codex 运行时必须调用 `submit_*` 工具才算完成，因此 `scripts/run_stock_agent.py:58-69` 追加了一段
说明去推翻正文：

> 上面的“只返回 JSON”描述的是你要形成的完整决策对象，不是最终聊天文本。

即 Prompt 正文仍是已废弃的 hermes 语义（`docs/codex-agent-runtime.md` 已说明 `scripts/hermes_*`
仅为回滚保留、不在当前 crontab 路径中）。先给规则再否定规则，容易引发不稳定行为。

修复：正文直接改为“形成完整决策对象并通过 `submit_*` 提交”，删除运行时补丁。

## P1-2 四类执行层硬门槛未在 Prompt 中披露

1. **禁买 `688` / `8` / `4` 开头代码**：`account/portfolio_policy.py:28` 定义，
   `scripts/execute_trade_signal.py:161` 与 `scripts/execute_live_trade_decision.py:243` 直接拒单。
   六个 Prompt/Policy 文件均未提及科创板与北交所不可买，选股环节也没有预过滤，模型可能选入 688 标的
   直到下单阶段才被拒。
2. **字段静默截断**：`reason` 180、`risk` 150、`market_view.summary` 100、`report.focus` 每条 80
   （`scripts/execute_trading_cycle.py:76-116`）。Prompt 只说“简洁精确”，不给数字；超长为静默截断而非
   报错，用户会看到断在半句的理由。
3. **`buy` / `add` 必须给出 `target_amount > 0`**，否则整轮抛错
   （`scripts/execute_trading_cycle.py:242-248`）。模拟盘 Prompt 未说明这是必填硬校验。
4. **实盘 action 白名单不含 `reduce` / `clear` / `add`**（`scripts/execute_trading_cycle.py:28`），
   但共享的 `agent_trading_policy.md` 多处讨论“减仓”。实盘若输出 `reduce`，`_normalize_rows` 抛错导致
   整轮零执行。共享策略与实盘白名单冲突且无任何标注。

## P1-3 `sell_pct` 示例值存在误导

`agent_simulated_trading_prompt.md` 的 JSON 模板写 `"sell_pct": 0.5`。执行层实际语义
（`scripts/execute_trade_signal.py:104-114`）为：

- `clear`：忽略 `sell_pct`，全部清仓；
- `sell` 缺省：1.0，即清仓；
- `reduce` 缺省：0.5。

模板把 `0.5` 放在通用示例位，模型倾向照抄，导致本应清仓的 `sell` 只卖一半。Prompt 从未说明不同 action
下的默认值差异。

修复：模板留空，并显式写出三种 action 的默认语义。

## P2-1 冗余与禁令过载

同一约束重复出现：T+1 可卖在 `agent_trading_policy.md` 与实盘 Prompt 各写一遍；`reviewed_codes`
完整性在共享策略与每个任务 Prompt 结尾各写一遍。

禁止类表述密度实测：

| 文件 | 字数 | 禁止类表述 |
| --- | --- | --- |
| `agent_trading_policy.md` | 3122 | 32 |
| `agent_stock_selection_prompt.md` | 1962 | 19 |
| `agent_stock_entry_policy.md` | 1465 | 12 |
| `agent_live_trading_prompt.md` | 1801 | 9 |
| `agent_simulated_trading_prompt.md` | 1779 | 7 |
| `agent_candidate_promotion_prompt.md` | 1039 | 7 |

操盘任务单轮注入约 6.4k 字、53 处禁令，但真正导致整轮失败的只有五条：`reviewed_codes` 全覆盖、
逐股一行、T+1 可卖、`target_amount` 必填、`entry_route` 合法。将它们平铺在 53 条“不得”中等于没有优先级。

修复：提取置顶的“硬失败清单”，其余降级为判断指引。

## P2-2 Prompt 缺少版本标识

执行层 schema 均带版本（`simulated_trading_decision.v1` 等），但 Prompt 文件无版本号，
`decision.json` 也不记录 Prompt 版本。修改 Prompt 后回看历史决策无法归因到具体版本，对每日运行 16 轮
并需要评估策略效果的系统构成审计缺口。

## 建议修复顺序

1. `selection` 注入 entry policy，删除选股 Prompt 内重复的路线定义（消除口径分裂）；
2. `candidate_lifecycle` 改为字段表述（消除无效工具调用）；
3. 批量上限 5 改为 20–40（降低超时率）；
4. 正文改为提交工具语义，删除 `run_stock_agent.py` 中的自我否认补丁；
5. 补充 688/8/4 禁买、各字段字数上限、实盘不支持 `reduce`；
6. `sell_pct` 模板留空并写清三种 action 的默认语义。

前三条改动量均在数行内，对稳定性与口径一致性收益最大。
