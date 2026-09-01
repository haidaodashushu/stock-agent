# Codex Agent 股票运行时

当前运行时将模型判断、确定性执行和消息投递分成三个边界：

1. `run_stock_agent.py` 启动一次临时的 Codex CLI 会话。
2. 每类任务只暴露本领域的 MCP 证据工具和一个受约束的 `submit_*` 工具。
3. 提交工具校验完整范围、`as_of`、账户隔离、T+1、持仓上限等硬规则后，才调用现有执行层。
4. 执行结果先写入幂等提交账本，再写消息 outbox；`lark-cli` 独立投递飞书。
5. 飞书入站由独立的 `stock-feishu-listener.service` 消费事件；先表情确认，再按消息幂等处理并回复，
   不与半小时交易、候选晋升或夜间选股的调度进程共用生命周期。

定时决策模型没有任意 SQL、任意数据库写入或券商接口。模拟盘可按既有规则自动成交；实盘仍只生成供人工核对的建议单。
飞书入站中的成交回报不经过模型，直接由受控本地事务回填；其他授权消息才进入独立 Codex 任务。

## 运行入口

```bash
scripts/stock_agent_selection_cycle.sh
scripts/stock_agent_trading_cycle.sh simulated
scripts/stock_agent_trading_cycle.sh live
scripts/stock_agent_candidate_promotion.sh
```

供应商入口集中在 `data/agent_runtime.py`。当前实现为 `codex-cli`，以后增加其他 CLI 时不应修改策略、MCP 或执行层。

## 调度与切换

```bash
.venv/bin/python scripts/switch_agent_runtime.py status
.venv/bin/python scripts/switch_agent_runtime.py preflight
.venv/bin/python scripts/switch_agent_runtime.py install
.venv/bin/python scripts/switch_agent_runtime.py rollback
```

`install` 会保留非股票 crontab，备份股票 Hermes 任务和 Web unit，暂停旧任务后安装 `config/stock-agent.crontab`。备份位于 `data/runtime/scheduler-backups/`；`rollback` 恢复原 crontab、Web unit、gateway 和原先启用的任务。

## 本地私有配置

复制 `config/runtime.example.json` 为 `config/runtime.local.json`，填写飞书 chat 或 user 目标。该文件被 git 忽略；也可用 `STOCK_RUNTIME_CONFIG` 指定其他位置。问财 keyring 使用 `~/.config/stock/iwencai_api_keys`。

飞书监听 service 按当前仓库路径渲染后安装，不在仓库中保存用户名或绝对路径：

```bash
.venv/bin/python scripts/install_feishu_listener_service.py
```

## 隔离验证

所有数据库入口支持 `STOCK_DB_PATH`。对一致性副本运行时，该路径会被显式转发给 Codex 启动的 MCP 子进程：

```bash
STOCK_DB_PATH=/path/to/shadow.db .venv/bin/python scripts/run_stock_agent.py \
  --task trading-simulated --stage 1100 --dry-run
```

生产成功与否以 `agent_decision_submissions` 的 durable 状态为准，不相信模型最终聊天文本。相同任务、模式和 `as_of` 只能认领一次；含糊失败不会自动重放。

## 六壬附注

实盘建议已经成功落库后，系统才使用项目内排盘脚本和断课指南生成白话附注。它看不到证券工具，失败不会改变方向、价格、数量、有效期或建议单状态。

## 兼容文件

`scripts/hermes_*` 暂时仅为回滚旧调度保留，不在当前 crontab 或 Web 路径中使用。确认观察期结束后可随旧运行时一起删除。
