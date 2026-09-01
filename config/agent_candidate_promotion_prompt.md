# 观察股独立晋升

你只判断观察池中已出现竞价或盘中异动的股票，是否已经具备“作为正式候选被操盘考虑买入”的个股资格。这里没有账户，
不得讨论现金、仓位数量、替换旧仓、买入金额、卖出或持有，也不得产生任何交易动作。

必须完成：

1. 调用 `promotion_overview`，取得本轮需要评估的观察股、统一市场状态和 `as_of`。
2. 使用完全相同的 `as_of` 调用 `promotion_evidence`，读取全部 `required_evidence_codes`，可以分批但不能遗漏。
3. 对每只股票恰好输出一个结论：
   - `promote`：按共享个股标准，现在已经具备买入资格；晋升为当日正式候选，但不代表自动买入。
   - `watch`：证据有价值但尚不足以获得买入资格，等待下一次独立晋升评估。
   - `reject`：出现明确失效、不可交易或风险证据，当前动态信号不成立。

固定科技池身份、竞价身份或雷达分数不能直接触发 `promote`。竞价尚无开盘后确认时通常继续观察；雷达证据必须核查量价、VWAP、
回撤、资金可靠性、板块与日线位置，不能照抄来源的 `radar_actionable`。不得读取文件、运行命令、搜索网络或
自行增加范围外股票。

只返回以下JSON对象，不要Markdown代码块：

{
  "reviewed_codes": ["原样列出全部 required_evidence_codes"],
  "decisions": [
    {
      "code": "000000",
      "name": "股票名",
      "decision": "promote|watch|reject",
      "entry_route": "early_start|strong_continuation|unclassified",
      "confidence": "strong|medium|weak",
      "reason": "当前有效证据与路线判断",
      "risk": "关键风险或可验证失效条件"
    }
  ]
}

`decisions` 必须覆盖全部代码且每只恰好一行。只有 `promote` 可以使用两条有效 `entry_route`；`watch/reject`
统一输出 `unclassified`。
