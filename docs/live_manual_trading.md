# 实盘操盘交易流程

目标：系统负责实盘操盘决策并生成建议单，用户在广发/同花顺手动执行买卖，成交后回填结果。系统**不保存真实账号密码、不自动提交真实订单**。

## 状态流转

`proposed` → `filled` / `cancelled` / `rejected` / `expired`

表：`live_trade_intents`

## 受控实盘操盘链路

cron 实盘任务必须走固定链路，便于审计每轮到底是 AI 判断、风控拒绝，还是没有动作：

1. 事实层：实盘 Cron 独立刷新实盘持仓和当日预选股，把事实覆盖写入两个表的 `mode=live`
   分区。模拟 Cron 只写 `mode=simulated`，两边不会覆盖彼此的 `as_of`。不创建 cycle_id，也不保存
   逐轮快照。

```bash
python3 scripts/refresh_trading_cycle.py --mode live --stage 1102
```

2. 读取层：模拟盘和实盘分别连接 `stock-simulated-trading` 与 `stock-live-trading` MCP。
   每套工具只返回本账户、持仓和近期动作；实盘近期动作包含待处理、已过期、已成交、已取消和已拒绝
   建议，避免建议过期后被 AI 误判为从未发生。两套 Cron 各自维护数据库版本和 `as_of`。

3. 决策层：实盘 Cron 独立调用 AI，复用 `config/agent_trading_policy.md` 并使用
   `config/agent_live_trading_prompt.md` 输出实盘专用决策 JSON。它看不到模拟账户或模拟动作，也不
   依赖模拟 Cron 是否成功。

```json
{
  "decisions": [
    {
      "code": "600000",
      "name": "示例股票",
      "action": "buy|sell|hold|watch|noop",
      "confidence": "strong|medium|weak",
      "price": 41.2,
      "volume": 100,
      "limit_price": 41.3,
      "expire_minutes": 15,
      "reason": "只引用快照事实的决策理由",
      "risk": "只引用快照事实和实盘风控的风险"
    }
  ]
}
```

4. 执行层：脚本重新从同一 `as_of` 的数据库状态构建校验上下文，并刷新一次执行价；版本变化、持仓
   遗漏、买入价高于限价、卖出价低于限价或其他风控失败都拒绝执行。实盘只生成手工建议单，不连接
   券商。

```bash
python3 scripts/execute_live_trade_decision.py \
  --decision-file logs/live_trade_decision_1100_latest.json
```

`buy/sell` 通过风控后写入 `live_trade_intents`；`hold/watch/noop` 只返回无建议单动作。

## 手动生成操盘建议单

```bash
python3 scripts/live_trade_intent.py propose buy 600000 示例股票 \
  --price 41.20 --volume 3000 \
  --reason "放量突破，策略建议建仓" \
  --strategy live_manual
```

输出文本可以直接发飞书给用户。

## 用户手动成交后回填

用户回复格式：

```text
成交 L20260101093000-DEMO01 10.00 100
```

回填：

```bash
python3 scripts/parse_live_fill.py "成交 L20260101093000-DEMO01 10.00 100"
```

或直接：

```bash
python3 scripts/live_trade_intent.py fill L20260101093000-DEMO01 --price 10.00 --volume 100
```

## 飞书自动回填

`stock-feishu-listener.service` 通过 `lark-cli event consume
im.message.receive_v1` 常驻接收消息。监听器只接受 `runtime.local.json` 中
`feishu.target_id` 对应的群，以及 `feishu.receive.allowed_sender_ids` 明确列出的发送者；
机器人消息、其他群和其他发送者均静默忽略。

处理顺序固定为：

1. 以渠道前缀加 `message_id` 写入 `bot_inbound_messages` 幂等账本；
2. 先给原消息添加 `OnIt` 表情；
3. 成交回报走本地确定性解析，一条消息中的多笔成交在同一 SQLite 事务内回填；
4. 其他消息交给独立、有限历史上下文的 Codex 任务；
5. 处理结果以机器人身份回复原消息。

支持一条消息回填多笔自然格式成交，例如：

```text
买入 600000 示例股票A：100 股，成交价 10.00
买入 000001 示例股票B：200 股，成交价 12.00
```

服务检查：

```bash
systemctl --user status stock-feishu-listener.service
journalctl --user -u stock-feishu-listener.service -f
lark-cli --profile stock event status --json
```

同一 `message_id` 不会重复执行；多笔回报中任意一笔无法安全匹配时，整条消息回滚并在飞书说明原因。

## 实盘影子账户规则

配置文件默认是 `config/live_manual_account.local.json`，也可通过
`STOCK_LIVE_ACCOUNT_CONFIG` 指定；可从同名 `.example.json` 复制初始化。

- 初始资金、后续资金流水和目标单票金额只读取本地账户配置，不在代码仓库中保存真实值
- 入金/出金记录在 `capital_flows`，只改变现金和累计投入本金，不计入交易盈亏
- 持仓数量、单票目标金额、单笔上限和不可交易板块均由本地配置决定
- 买入/建议数量必须 100 股整数倍
- 建议有效期由决策策略和本地配置共同约束

## 策略口径

核心原则：**低位多看逻辑变化，高位多看趋势量价**。

- 模拟盘和实盘共享市场策略原则，但分别读取账户、持仓和近期动作并独立决策。
- 实盘不得复用、推测或依赖模拟盘信号。
- 低位标的：可以重视产业/政策/订单/技术路线/景气变化，但静态概念只算“主线匹配”；建议买入前必须有弱/中/强逻辑变化证据，并至少有止跌、放量试盘、金叉、放量启动等技术/量能确认。
- 高位标的：允许结合行情顺势追高，重点量化多周期趋势、相对强弱、半小时量价、VWAP和回撤幅度；
  强势行情可以跟随持续增强的趋势，弱市或震荡市更关注冲高回落和趋势衰减，不因题材热度或绝对涨幅
  单独下结论。
- 实盘层独立风控：使用本地账户配置，不照搬模拟盘仓位比例。
- 建议单理由必须写清：属于“主线匹配”“弱/中/强逻辑变化”还是“高位趋势量价确认”。

## 当前边界

- 该流程记录系统操盘建议单和用户真实手工成交回报。
- 不读取真实广发账户资金/持仓。
- 不自动提交真实订单。
- 如需与模拟盘或影子实盘账户同步，需要单独做对账/影子账户模块。
