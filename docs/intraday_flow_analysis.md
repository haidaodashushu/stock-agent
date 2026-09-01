# 盘中区间量价承接分析

## 定位

`scripts/analyze_intraday_flow.py` 用腾讯当日分钟行情分析任意盘中区间的价格反应、双边成交额和后续表现。核心计算位于 `data/intraday_flow.py`，可被选股、盯盘和复盘代码直接调用。

本工具输出的是 **区间量价承接代理**，不是交易所认证的主力净流入：

- `turnover_amount` 是该区间买卖双方的总成交额；
- 价格同步上涨只表示该时段买方更主动；
- 不能从总成交额推断机构、散户、国家队或其他参与者身份；
- 如需“大单/主力净流入”口径，应与 `FundFlowFilter` 的独立数据交叉验证。

## 数据合同

对 `[start, end]` 以及可选的 `follow_end`，每只证券输出：

| 字段 | 含义 |
|---|---|
| `trading_date` | 腾讯分钟行情所属交易日，用于阻止周末/节假日误用旧数据 |
| `start_time/end_time` | 实际采用的分钟点；起点取边界后首点，终点取边界前末点 |
| `price_change_pct` | 起点到终点涨跌幅 |
| `turnover_amount` | 区间双边成交额 |
| `turnover_observed_pct` | 区间成交额占抓取时已观测累计成交额比例；收盘后运行才等于全天占比 |
| `follow_change_pct` | 终点到后续观察点涨跌幅，负值表示回吐 |
| `net_change_pct` | 起点到后续观察点净涨跌幅 |
| `retention_ratio` | `net_change_pct / price_change_pct`；仅在区间涨跌非零时有效 |
| `amount_mode` | `cumulative` 或 `incremental`；腾讯分钟接口使用累计口径 |

板块聚合使用 `turnover_amount` 作为权重。板块可重叠，因此不同板块成交额不能直接相加为全市场成交额。

## 命令示例

分析指定股票：

```bash
.venv/bin/python scripts/analyze_intraday_flow.py \
  --codes 300308,300502,600584,688256 \
  --start 13:47 \
  --end 14:15 \
  --follow-end 15:00
```

扫描本地最新A股池中当日成交额前100名，并追加主要宽基ETF：

```bash
.venv/bin/python scripts/analyze_intraday_flow.py \
  --top-by-turnover 100 \
  --include-default-etfs \
  --start 13:47 \
  --end 14:15 \
  --follow-end 15:00 \
  --sort-by turnover \
  --json-out reports/intraday-flow.json
```

使用自定义板块映射：

```json
{
  "CPO/光通信": ["300308", "300502", "300394"],
  "半导体/存储/设备": ["600584", "603986", "688256", "688008"]
}
```

```bash
.venv/bin/python scripts/analyze_intraday_flow.py \
  --codes 300308,300502,300394,600584,603986,688256,688008 \
  --start 13:47 --end 14:15 --follow-end 15:00 \
  --groups-file /path/to/groups.json
```

也可用 `--groups-from-db` 聚合 `concepts` 表已有成员。该表目前覆盖不完整，正式策略使用前必须检查样本数。

## Python接口

```python
from data.fetcher.tencent_quote import TencentQuoteFetcher
from data.intraday_flow import analyze_interval

frame = TencentQuoteFetcher().fetch_minute("300308")
result = analyze_interval(
    frame,
    "13:47",
    "14:15",
    follow_end="15:00",
    amount_mode="cumulative",
)
```

批量调用使用 `analyze_many()`；板块聚合使用 `aggregate_groups()`。

## 操盘与选股接入建议

### 盘中操盘

只对当前持仓、候选和高成交活跃股计算，避免每半小时全市场请求：

1. 最近30分钟价格上涨且区间成交额占比提升：主动承接候选；
2. 上涨后 `retention_ratio` 高：承接较有效；
3. 大幅反抽但 `follow_change_pct` 显著为负：抢反弹失败或抛压未尽；
4. 跌停打开但收盘重新封死：不能因区间成交额大而判定资金净流入；
5. 与日线位置、VWAP、消息、主力净流入和板块广度联合使用。

### 选股

盘前选股不能使用尚未发生的当日分钟数据。适合使用的场景是：

- 前一交易日尾盘承接作为次日候选因子；
- 开盘后对盘前候选做二次确认；
- 大跌日识别“反抽保留率高”的强承接标的；
- 收盘复盘评估反抽是持续承接还是流动性修复。

在进入自动交易评分前，应先把原始字段保存到筛选历史，做样本外回测或至少滚动评估；不要直接把一次行情观察固化为买入规则。

## 解释模板

推荐同时报告四项：

1. 区间涨跌；
2. 区间成交额及截至抓取时点的累计成交占比（收盘后运行才称全天占比）；
3. 后续回吐；
4. 保留率。

例如：

> 13:47—14:15上涨8.5%，区间成交54亿元、占全天9.6%；14:15后回吐5.2%，最终仅保留约34%的反抽幅度。说明该时段买方积极，但尾盘抛压仍强，不能据此认定趋势反转。

## 限制与校验

- 腾讯分钟接口只提供最近交易日的当日分时，不是历史分钟数据库；工具默认要求 `trading_date` 等于上海当前日期。
- 复盘旧交易日需要已保存的分钟数据或支持历史分钟线的数据源；不要使用 `--allow-date-mismatch` 后把最近交易日误报为指定历史日期。
- 免费接口可能超时；批量工具支持并发和重试，并在 JSON `errors` 中保留失败标的。
- `retention_ratio` 在反抽接近0时没有稳定解释，工具返回空值。
- 板块聚合质量取决于成分股覆盖与分类时效。

## 验证

```bash
.venv/bin/python -m unittest \
  tests.test_intraday_interval_flow \
  tests.test_intraday_minute_summary \
  tests.test_tencent_quote_symbols -v
```
