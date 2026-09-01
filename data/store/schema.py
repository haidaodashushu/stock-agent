"""
data/store/schema.py - SQLite 数据库表结构
"""
from typing import List

# SQLite 数据库文件名
DB_NAME = "stock_data.db"

# ========== 表结构定义 ==========

CREATE_TABLES = """
-- 股票基本信息表
CREATE TABLE IF NOT EXISTS stocks (
    code        TEXT PRIMARY KEY,        -- 股票代码 (000001)
    name        TEXT NOT NULL,           -- 股票名称
    exchange    TEXT DEFAULT '',         -- 交易所 (sh/sz/bj)
    industry    TEXT DEFAULT '',         -- 所属行业
    market_cap  REAL DEFAULT 0,         -- 市值
    list_date   TEXT DEFAULT '',         -- 上市日期
    is_active   INTEGER DEFAULT 1,      -- 是否活跃（退市=0）
    updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 日K线数据表
CREATE TABLE IF NOT EXISTS daily_prices (
    code        TEXT NOT NULL,           -- 股票代码
    date        TEXT NOT NULL,           -- 日期 YYYY-MM-DD
    open        REAL DEFAULT 0,         -- 开盘价
    close       REAL DEFAULT 0,         -- 收盘价
    high        REAL DEFAULT 0,         -- 最高价
    low         REAL DEFAULT 0,         -- 最低价
    volume      INTEGER DEFAULT 0,      -- 成交量(股)
    amount      REAL DEFAULT 0,         -- 成交额(元)
    adjust_flag TEXT DEFAULT 'qfq',     -- 复权类型: qfq(前复权) hfq(后复权)
    updated_at  TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (code, date, adjust_flag)
);

-- 实时行情快照表（每次拉取的快照）
CREATE TABLE IF NOT EXISTS realtime_snapshots (
    code        TEXT NOT NULL,
    name        TEXT DEFAULT '',
    price       REAL DEFAULT 0,
    open        REAL DEFAULT 0,
    high        REAL DEFAULT 0,
    low         REAL DEFAULT 0,
    prev_close  REAL DEFAULT 0,
    change_pct  REAL DEFAULT 0,          -- 涨跌幅
    volume      INTEGER DEFAULT 0,
    amount      REAL DEFAULT 0,
    snapshot_at TEXT NOT NULL,           -- 快照时间
    source      TEXT DEFAULT 'sina',
    PRIMARY KEY (code, snapshot_at)
);

-- 新闻表
CREATE TABLE IF NOT EXISTS news (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    content     TEXT DEFAULT '',
    source      TEXT DEFAULT '',
    publish_at  TEXT DEFAULT '',
    link        TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 概念/板块表
CREATE TABLE IF NOT EXISTS concepts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,    -- 板块名称
    category    TEXT DEFAULT 'concept',  -- concept/industry
    stocks      TEXT DEFAULT '',         -- 成分股代码(逗号分隔)
    updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 交易信号表
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    signal_type TEXT NOT NULL,           -- buy/sell
    strategy    TEXT NOT NULL,           -- 策略名称
    price       REAL DEFAULT 0,
    reason      TEXT DEFAULT '',
    strength    REAL DEFAULT 0,          -- 信号强度 0-1
    created_at  TEXT DEFAULT (datetime('now', 'localtime')),
    executed    INTEGER DEFAULT 0        -- 是否已执行
);

-- 持仓表（兼容 SimTrader 格式）
CREATE TABLE IF NOT EXISTS portfolio (
    code        TEXT PRIMARY KEY,
    name        TEXT DEFAULT '',
    volume      INTEGER DEFAULT 0,
    cost_price  REAL DEFAULT 0,
    current_price REAL DEFAULT 0,
    market_value REAL DEFAULT 0,
    profit      REAL DEFAULT 0,
    profit_pct  REAL DEFAULT 0,
    high_since_entry REAL DEFAULT 0,
    available   INTEGER DEFAULT 0,
    updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 订单/交易记录表（兼容 SimTrader 格式）
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT,
    code        TEXT NOT NULL,
    name        TEXT DEFAULT '',
    direction   TEXT NOT NULL,
    price       REAL DEFAULT 0,
    volume      INTEGER DEFAULT 0,
    amount      REAL DEFAULT 0,
    commission  REAL DEFAULT 0,
    tax         REAL DEFAULT 0,
    status      TEXT DEFAULT 'filled',
    reason      TEXT DEFAULT '',
    strategy    TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 回测结果表
CREATE TABLE IF NOT EXISTS backtest_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy    TEXT NOT NULL,
    code        TEXT DEFAULT '',
    start_date  TEXT DEFAULT '',
    end_date    TEXT DEFAULT '',
    total_return REAL DEFAULT 0,
    annual_return REAL DEFAULT 0,
    max_drawdown REAL DEFAULT 0,
    win_rate    REAL DEFAULT 0,
    sharpe      REAL DEFAULT 0,
    trade_count INTEGER DEFAULT 0,
    detail      TEXT DEFAULT '{}',       -- JSON 详细交易记录
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily_prices(code, date);
CREATE INDEX IF NOT EXISTS idx_signals_code ON signals(code);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_news_title ON news(title);

-- 每日候选池：盘前/夜间选股覆盖目标交易日，只保留每只股票最新结果
CREATE TABLE IF NOT EXISTS screen_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT NOT NULL,
    run_time    TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT DEFAULT '',
    price       REAL DEFAULT 0,
    score       REAL DEFAULT 0,
    signal_type TEXT DEFAULT 'watch',
    strategies  TEXT DEFAULT '',
    concepts    TEXT DEFAULT '',
    trend       TEXT DEFAULT '',
    pct_change  REAL DEFAULT 0,
    vol_ratio   REAL DEFAULT 0,
    extra       TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(run_date, code)
);
CREATE INDEX IF NOT EXISTS idx_screen_records_date_score
ON screen_records(run_date, score DESC);

-- 每次选股结果的轻量历史，用于比较夜间预选、盘前刷新和事后表现。
-- 当前操盘仍只读取 screen_records；这里不使用 cycle_id。
CREATE TABLE IF NOT EXISTS screen_record_history (
    run_date    TEXT NOT NULL,
    run_time    TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT DEFAULT '',
    price       REAL DEFAULT 0,
    score       REAL DEFAULT 0,
    signal_type TEXT DEFAULT 'watch',
    strategies  TEXT DEFAULT '',
    concepts    TEXT DEFAULT '',
    trend       TEXT DEFAULT '',
    pct_change  REAL DEFAULT 0,
    vol_ratio   REAL DEFAULT 0,
    extra       TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (run_date, run_time, code)
);
CREATE INDEX IF NOT EXISTS idx_screen_record_history_run
ON screen_record_history(run_date, run_time, score DESC);

-- AI 最终选股的当前候选快照。脚本每轮原子覆盖 slot=1 和候选明细；
-- AI 只能通过只读 MCP 按 as_of 读取，不能直接执行 SQL 或写库。
CREATE TABLE IF NOT EXISTS screen_candidate_state (
    slot                INTEGER PRIMARY KEY CHECK (slot = 1),
    as_of               TEXT NOT NULL UNIQUE,
    run_date            TEXT NOT NULL,
    run_time            TEXT NOT NULL,
    run_label           TEXT DEFAULT '',
    target              TEXT DEFAULT '',
    expected_daily_date TEXT DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'ready'
                        CHECK (status IN ('ready', 'selected', 'failed')),
    candidate_count     INTEGER DEFAULT 0,
    selected_count      INTEGER DEFAULT 0,
    market_context      TEXT DEFAULT '{}',
    error               TEXT DEFAULT '',
    created_at          TEXT DEFAULT (datetime('now','localtime')),
    completed_at        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS screen_candidate_pool (
    as_of       TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT DEFAULT '',
    price       REAL DEFAULT 0,
    quant_score REAL DEFAULT 0,
    signal_type TEXT DEFAULT 'watch',
    trend       TEXT DEFAULT '',
    pct_change  REAL DEFAULT 0,
    vol_ratio   REAL DEFAULT 0,
    zone        TEXT DEFAULT '',
    route       TEXT DEFAULT '',
    theme_group TEXT DEFAULT '',
    evidence    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (as_of, code)
);
CREATE INDEX IF NOT EXISTS idx_screen_candidate_pool_rank
ON screen_candidate_pool(as_of, rank);

-- 半小时操盘个股当前状态。模拟盘与实盘各自覆盖，不保存周期快照。
CREATE TABLE IF NOT EXISTS trading_stock_state (
    mode            TEXT NOT NULL CHECK (mode IN ('simulated', 'live')),
    code            TEXT NOT NULL,
    name            TEXT DEFAULT '',
    is_candidate    INTEGER DEFAULT 0,
    is_sim_holding  INTEGER DEFAULT 0,
    is_live_holding INTEGER DEFAULT 0,
    screen_date     TEXT DEFAULT '',
    screen_score    REAL DEFAULT 0,
    screen_signal   TEXT DEFAULT '',
    payload         TEXT NOT NULL DEFAULT '{}',
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (mode, code)
);
CREATE INDEX IF NOT EXISTS idx_trading_stock_state_scope
ON trading_stock_state(mode, is_candidate, is_sim_holding, is_live_holding);

-- 半小时操盘市场与账户当前状态。每个账户模式只有一行。
CREATE TABLE IF NOT EXISTS trading_market_state (
    mode        TEXT PRIMARY KEY CHECK (mode IN ('simulated', 'live')),
    payload     TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL
);

-- 操盘共用资金流缓存。实时供应商短暂失败时可回退到最近一次成功结果，
-- 但调用方必须通过 status/observed_at 判断新鲜度，不能把缓存冒充实时值。
CREATE TABLE IF NOT EXISTS fund_flow_cache (
    code        TEXT PRIMARY KEY,
    trade_date  TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    source      TEXT DEFAULT '',
    observed_at TEXT NOT NULL,
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_fund_flow_cache_observed
ON fund_flow_cache(observed_at);

-- 规范化个股—板块关系。每个来源按股票全量替换自己的快照，避免旧概念
-- 只增不减造成错误匹配。
CREATE TABLE IF NOT EXISTS stock_sector_membership (
    code        TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    sector_type TEXT NOT NULL CHECK (sector_type IN ('industry', 'concept')),
    source      TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (code, sector_name, source)
);

CREATE INDEX IF NOT EXISTS idx_stock_sector_membership_code
ON stock_sector_membership(code, sector_type);

-- 即使供应商成功返回“无归属”也记录刷新状态，避免每半小时重复请求。
CREATE TABLE IF NOT EXISTS stock_sector_profile_state (
    code         TEXT NOT NULL,
    source       TEXT NOT NULL,
    refreshed_at TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ok',
    error        TEXT DEFAULT '',
    PRIMARY KEY (code, source)
);

-- 持仓/候选股新闻事件表（去重后用于策略因子与告警）
CREATE TABLE IF NOT EXISTS news_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    title TEXT NOT NULL,
    content TEXT DEFAULT '',
    source TEXT DEFAULT '',
    publish_at TEXT DEFAULT '',
    url TEXT DEFAULT '',
    category TEXT DEFAULT 'news',
    sentiment TEXT DEFAULT 'neutral',
    score REAL DEFAULT 0,
    risk_level TEXT DEFAULT 'low',
    tags TEXT DEFAULT '',
    pushed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(code, title, publish_at, category)
);

CREATE INDEX IF NOT EXISTS idx_news_events_code_time ON news_events(code, publish_at);
CREATE INDEX IF NOT EXISTS idx_news_events_score ON news_events(score);

-- 候选增强刷新游标：成功但无新闻也需要记录，避免盘前反复慢查询。
CREATE TABLE IF NOT EXISTS candidate_intelligence_refresh (
    code         TEXT NOT NULL,
    source       TEXT NOT NULL,
    refreshed_at TEXT NOT NULL,
    status       TEXT DEFAULT 'ok',
    detail       TEXT DEFAULT '',
    PRIMARY KEY (code, source)
);

-- 财务因子（BaoStock/akshare等来源统一落库）
CREATE TABLE IF NOT EXISTS financial_factors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    period TEXT NOT NULL,
    roe REAL DEFAULT 0,
    roa REAL DEFAULT 0,
    gross_margin REAL DEFAULT 0,
    net_margin REAL DEFAULT 0,
    eps REAL DEFAULT 0,
    revenue_yoy REAL DEFAULT 0,
    profit_yoy REAL DEFAULT 0,
    debt_ratio REAL DEFAULT 0,
    source TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(code, period, source)
);

CREATE INDEX IF NOT EXISTS idx_financial_factors_code_period ON financial_factors(code, period);

-- LLM 财报/MD&A 解析评分：中期“好赛道/好公司”因子，不直接触发盘中交易
CREATE TABLE IF NOT EXISTS fundamental_llm_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    period TEXT NOT NULL,                 -- 2025A / 2025H1 / 2026Q2 等
    report_type TEXT DEFAULT '',          -- annual / semiannual / quarterly
    report_date TEXT DEFAULT '',          -- 报告披露或财报期日期
    industry_demand_score REAL DEFAULT 2.5,      -- 1-5，报告期行业需求
    future_demand_score REAL DEFAULT 2.5,        -- 1-5，未来行业需求判断
    product_penetration_score REAL DEFAULT 2.5,  -- 1-5，产品渗透率/空间
    strategy_score REAL DEFAULT 1.5,             -- 0-3，战略合理性
    candor_score REAL DEFAULT 0.5,               -- 0/1，管理层坦诚度
    composite_score REAL DEFAULT 2.5,            -- 0-5，综合分
    confidence REAL DEFAULT 0.0,                 -- 0-1，证据置信度
    summary TEXT DEFAULT '',
    evidence TEXT DEFAULT '{}',                  -- JSON，引用片段/理由
    source TEXT DEFAULT '',
    model TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(code, period, source, model)
);

CREATE INDEX IF NOT EXISTS idx_fundamental_llm_scores_code_period
ON fundamental_llm_scores(code, period, report_date);

-- 价值投资事实快照/规则评分：只用于观察池、候选排序和AI解释输入，不直接触发交易
CREATE TABLE IF NOT EXISTS value_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    as_of TEXT NOT NULL,
    company_type TEXT DEFAULT 'unknown',
    value_label TEXT DEFAULT 'unknown',
    watch_pool INTEGER DEFAULT 0,
    business_quality_score REAL DEFAULT 0,
    financial_quality_score REAL DEFAULT 0,
    growth_credibility_score REAL DEFAULT 0,
    valuation_margin_score REAL DEFAULT 0,
    trap_risk_score REAL DEFAULT 0,
    composite_score REAL DEFAULT 0,
    confidence REAL DEFAULT 0,
    facts TEXT DEFAULT '{}',
    rule_summary TEXT DEFAULT '',
    ai_prompt_path TEXT DEFAULT '',
    source TEXT DEFAULT 'value_snapshot.py',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(code, as_of, source)
);

CREATE INDEX IF NOT EXISTS idx_value_snapshots_code_time
ON value_snapshots(code, as_of);

CREATE INDEX IF NOT EXISTS idx_value_snapshots_composite
ON value_snapshots(composite_score, value_label);

-- 价值投资分层股票池：控制深度数据刷新范围，避免全市场重数据拉取
CREATE TABLE IF NOT EXISTS value_universe (
    code TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'candidate',      -- core/candidate/temp/basic
    priority INTEGER DEFAULT 50,                 -- 越大越优先刷新
    reasons TEXT DEFAULT '[]',                   -- JSON，进入池子的来源/理由
    sources TEXT DEFAULT '[]',                   -- JSON，portfolio/latest_screen/user_query 等
    status TEXT DEFAULT 'active',
    first_seen_at TEXT DEFAULT (datetime('now','localtime')),
    last_seen_at TEXT DEFAULT (datetime('now','localtime')),
    last_refreshed_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_value_universe_tier_priority
ON value_universe(tier, priority, last_refreshed_at);

-- 价值数据新鲜度：按数据类型记录最近尝试/成功时间，过期才刷新
CREATE TABLE IF NOT EXISTS value_data_freshness (
    code TEXT NOT NULL,
    data_type TEXT NOT NULL,                     -- valuation/financial/news/llm/value_snapshot
    last_success_at TEXT DEFAULT '',
    last_attempt_at TEXT DEFAULT '',
    status TEXT DEFAULT 'missing',               -- ok/missing/stale/error/skipped
    source TEXT DEFAULT '',
    error TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (code, data_type)
);

CREATE INDEX IF NOT EXISTS idx_value_data_freshness_due
ON value_data_freshness(data_type, status, last_success_at);

-- 模拟账户状态
CREATE TABLE IF NOT EXISTS account_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    available_cash REAL DEFAULT 1000000,
    total_equity REAL DEFAULT 1000000,
    total_profit REAL DEFAULT 0,
    total_commission REAL DEFAULT 0,
    total_tax REAL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 每日权益快照
CREATE TABLE IF NOT EXISTS daily_equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    total_equity REAL,
    available_cash REAL,
    market_value REAL,
    total_profit REAL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 实盘操盘建议单：系统负责操盘决策并生成建议单，用户手动在券商/同花顺执行后回填成交。
CREATE TABLE IF NOT EXISTS live_trade_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    action TEXT NOT NULL,                 -- buy/sell
    suggested_price REAL DEFAULT 0,
    suggested_volume INTEGER DEFAULT 0,
    suggested_amount REAL DEFAULT 0,
    limit_price REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    strategy TEXT DEFAULT '',
    risk_note TEXT DEFAULT '',
    status TEXT DEFAULT 'proposed',       -- proposed/filled/cancelled/rejected/expired
    created_at TEXT DEFAULT (datetime('now','localtime')),
    expires_at TEXT DEFAULT '',
    notified_at TEXT DEFAULT '',
    filled_price REAL DEFAULT 0,
    filled_volume INTEGER DEFAULT 0,
    filled_amount REAL DEFAULT 0,
    filled_at TEXT DEFAULT '',
    user_note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_live_trade_intents_status ON live_trade_intents(status, created_at);
CREATE INDEX IF NOT EXISTS idx_live_trade_intents_code ON live_trade_intents(code, created_at);

-- 固定战略池盘中雷达。每轮保留少量候选及可审计的触发证据；
-- 全池轻量行情快照复用 realtime_snapshots。
CREATE TABLE IF NOT EXISTS intraday_radar_runs (
    as_of             TEXT PRIMARY KEY,
    status            TEXT NOT NULL DEFAULT 'ready',
    pool_size         INTEGER DEFAULT 0,
    quote_count       INTEGER DEFAULT 0,
    prefiltered_count INTEGER DEFAULT 0,
    selected_count    INTEGER DEFAULT 0,
    market_context    TEXT DEFAULT '{}',
    error             TEXT DEFAULT '',
    created_at        TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS intraday_radar_candidates (
    as_of          TEXT NOT NULL,
    rank           INTEGER NOT NULL,
    code           TEXT NOT NULL,
    name           TEXT DEFAULT '',
    theme_group    TEXT DEFAULT '',
    score          REAL DEFAULT 0,
    price          REAL DEFAULT 0,
    change_pct     REAL DEFAULT 0,
    triggers       TEXT DEFAULT '[]',
    risk_tags      TEXT DEFAULT '[]',
    evidence       TEXT DEFAULT '{}',
    expires_at     TEXT NOT NULL,
    PRIMARY KEY (as_of, code)
);
CREATE INDEX IF NOT EXISTS idx_intraday_radar_candidates_expiry
ON intraday_radar_candidates(expires_at, rank);

-- 开盘集合竞价事实与短时观察候选。竞价候选只扩充决策范围，
-- 不直接获得买入资格；开盘后的盘中雷达负责再次确认。
CREATE TABLE IF NOT EXISTS opening_auction_runs (
    trade_date       TEXT NOT NULL,
    phase            TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'ready',
    scope_count      INTEGER DEFAULT 0,
    tencent_count    INTEGER DEFAULT 0,
    iwencai_count    INTEGER DEFAULT 0,
    started_at       TEXT DEFAULT '',
    completed_at     TEXT DEFAULT '',
    error            TEXT DEFAULT '',
    PRIMARY KEY (trade_date, phase)
);

CREATE TABLE IF NOT EXISTS opening_auction_snapshots (
    trade_date          TEXT NOT NULL,
    phase               TEXT NOT NULL,
    code                TEXT NOT NULL,
    name                TEXT DEFAULT '',
    observed_at         TEXT NOT NULL,
    provider_time       TEXT DEFAULT '',
    provider_current    INTEGER DEFAULT 0,
    previous_close      REAL DEFAULT 0,
    last_price          REAL DEFAULT 0,
    open_price          REAL DEFAULT 0,
    reported_volume_raw REAL DEFAULT 0,
    reported_amount_raw REAL DEFAULT 0,
    bid_levels          TEXT DEFAULT '[]',
    ask_levels          TEXT DEFAULT '[]',
    raw_fields          TEXT DEFAULT '[]',
    source              TEXT NOT NULL DEFAULT 'tencent',
    PRIMARY KEY (trade_date, phase, code, source)
);
CREATE INDEX IF NOT EXISTS idx_opening_auction_snapshots_code_date
ON opening_auction_snapshots(code, trade_date, phase);

CREATE TABLE IF NOT EXISTS opening_auction_final (
    trade_date              TEXT NOT NULL,
    code                    TEXT NOT NULL,
    name                    TEXT DEFAULT '',
    auction_price           REAL DEFAULT 0,
    auction_change_pct      REAL DEFAULT 0,
    matched_volume_shares   REAL DEFAULT 0,
    matched_amount_yuan     REAL DEFAULT 0,
    unmatched_volume_signed REAL DEFAULT 0,
    unmatched_amount_signed REAL DEFAULT 0,
    anomaly_type            TEXT DEFAULT '',
    anomaly_note            TEXT DEFAULT '',
    rating                  TEXT DEFAULT '',
    observed_at             TEXT NOT NULL,
    raw_payload             TEXT DEFAULT '{}',
    source                  TEXT NOT NULL DEFAULT 'iwencai',
    PRIMARY KEY (trade_date, code, source)
);
CREATE INDEX IF NOT EXISTS idx_opening_auction_final_date_amount
ON opening_auction_final(trade_date, matched_amount_yuan DESC);

CREATE TABLE IF NOT EXISTS opening_auction_watch_candidates (
    trade_date      TEXT NOT NULL,
    rank            INTEGER NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT DEFAULT '',
    theme_group     TEXT DEFAULT '',
    score           REAL DEFAULT 0,
    auction_price   REAL DEFAULT 0,
    change_pct      REAL DEFAULT 0,
    triggers        TEXT DEFAULT '[]',
    risk_tags       TEXT DEFAULT '[]',
    evidence        TEXT DEFAULT '{}',
    generated_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    rule_version    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_opening_auction_watch_expiry
ON opening_auction_watch_candidates(expires_at, rank);

-- 竞价/雷达动态观察的独立AI晋升记录。晋升只授予当日候选资格，
-- 不读取账户、不产生订单，正常操盘继续决定是否实际买入。
CREATE TABLE IF NOT EXISTS candidate_promotion_runs (
    as_of              TEXT PRIMARY KEY,
    trade_date         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ready',
    source_fingerprint TEXT NOT NULL,
    candidate_count    INTEGER DEFAULT 0,
    promoted_count     INTEGER DEFAULT 0,
    response           TEXT DEFAULT '{}',
    error              TEXT DEFAULT '',
    created_at         TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_candidate_promotion_runs_date
ON candidate_promotion_runs(trade_date, as_of DESC);

CREATE TABLE IF NOT EXISTS candidate_promotion_decisions (
    as_of          TEXT NOT NULL,
    trade_date     TEXT NOT NULL,
    code           TEXT NOT NULL,
    name           TEXT DEFAULT '',
    decision       TEXT NOT NULL CHECK (decision IN ('promote','watch','reject')),
    entry_route    TEXT DEFAULT 'unclassified',
    confidence     TEXT DEFAULT 'weak',
    reason         TEXT DEFAULT '',
    risk           TEXT DEFAULT '',
    source_types   TEXT DEFAULT '[]',
    evidence       TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (as_of, code)
);
CREATE INDEX IF NOT EXISTS idx_candidate_promotion_decisions_date_code
ON candidate_promotion_decisions(trade_date, code, as_of DESC);

CREATE TABLE IF NOT EXISTS intraday_candidate_promotions (
    trade_date     TEXT NOT NULL,
    code           TEXT NOT NULL,
    name           TEXT DEFAULT '',
    entry_route    TEXT NOT NULL CHECK (entry_route IN ('early_start','strong_continuation')),
    confidence     TEXT DEFAULT 'medium',
    promoted_at    TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    source_types   TEXT DEFAULT '[]',
    reason         TEXT DEFAULT '',
    risk           TEXT DEFAULT '',
    evidence       TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'active',
    updated_at     TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_intraday_candidate_promotions_active
ON intraday_candidate_promotions(trade_date, status, expires_at);

-- 独立正式候选池。来源任务只写各自信号，候选管理器原子生成版本；
-- 操盘只读取 ready 快照，不在决策过程中合并或替换候选。
CREATE TABLE IF NOT EXISTS candidate_board_runs (
    as_of              TEXT PRIMARY KEY,
    trade_date         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ready',
    active_limit       INTEGER NOT NULL DEFAULT 12,
    active_count       INTEGER DEFAULT 0,
    reserve_count      INTEGER DEFAULT 0,
    morning_count      INTEGER DEFAULT 0,
    auction_count      INTEGER DEFAULT 0,
    radar_count        INTEGER DEFAULT 0,
    source_versions    TEXT DEFAULT '{}',
    source_fingerprint TEXT NOT NULL,
    error              TEXT DEFAULT '',
    created_at         TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_candidate_board_runs_date
ON candidate_board_runs(trade_date, as_of DESC);

CREATE TABLE IF NOT EXISTS candidate_board_members (
    as_of              TEXT NOT NULL,
    state              TEXT NOT NULL,
    rank               INTEGER NOT NULL,
    code               TEXT NOT NULL,
    name               TEXT DEFAULT '',
    primary_source     TEXT DEFAULT '',
    source_types       TEXT DEFAULT '[]',
    buy_eligible       INTEGER DEFAULT 0,
    replaced_code      TEXT DEFAULT '',
    replacement_reason TEXT DEFAULT '',
    expires_at         TEXT DEFAULT '',
    payload            TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (as_of, code)
);
CREATE INDEX IF NOT EXISTS idx_candidate_board_members_state
ON candidate_board_members(as_of, state, rank);

-- 跨日候选发现快照。每日全市场量化池只是发现源，不再因为下一次
-- screen_records 排名变化而丢失一只股票的准备/升温过程。
CREATE TABLE IF NOT EXISTS candidate_discovery_history (
    as_of              TEXT NOT NULL,
    evidence_date      TEXT NOT NULL,
    target_trade_date  TEXT NOT NULL,
    rank               INTEGER NOT NULL,
    code               TEXT NOT NULL,
    name               TEXT DEFAULT '',
    entry_route        TEXT DEFAULT '',
    setup_stage        TEXT DEFAULT 'preparing',
    setup_score        REAL DEFAULT 0,
    final_score        REAL DEFAULT 0,
    buy_eligible       INTEGER DEFAULT 0,
    evidence           TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (as_of, code)
);
CREATE INDEX IF NOT EXISTS idx_candidate_discovery_date_code
ON candidate_discovery_history(evidence_date, code);

-- 每只股票一个可恢复的跨日观察状态。它只为每日最终选股提供历史证据，
-- 不能直接进入正式候选或授予买入资格。
CREATE TABLE IF NOT EXISTS candidate_lifecycle (
    code                 TEXT PRIMARY KEY,
    name                 TEXT DEFAULT '',
    state                TEXT NOT NULL DEFAULT 'preparing'
                         CHECK (state IN ('preparing','warming','actionable','cooling','invalidated','expired')),
    entry_route          TEXT DEFAULT '',
    first_seen_date      TEXT NOT NULL,
    last_seen_date       TEXT NOT NULL,
    last_evidence_date   TEXT NOT NULL,
    last_improved_date   TEXT DEFAULT '',
    observation_sessions INTEGER DEFAULT 1,
    improving_streak     INTEGER DEFAULT 0,
    stale_sessions       INTEGER DEFAULT 0,
    previous_score       REAL DEFAULT 0,
    current_score        REAL DEFAULT 0,
    best_score           REAL DEFAULT 0,
    setup_score          REAL DEFAULT 0,
    buy_eligible         INTEGER DEFAULT 0, -- 兼容旧库，v2始终为0
    invalidation_reason  TEXT DEFAULT '',
    payload              TEXT NOT NULL DEFAULT '{}',
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_lifecycle_state_score
ON candidate_lifecycle(state, setup_score DESC, current_score DESC);

CREATE TABLE IF NOT EXISTS candidate_lifecycle_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT NOT NULL,
    evidence_date  TEXT NOT NULL,
    from_state     TEXT DEFAULT '',
    to_state       TEXT NOT NULL,
    reason         TEXT DEFAULT '',
    payload        TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_lifecycle_events_code
ON candidate_lifecycle_events(code, evidence_date, id);

-- Agent 决策提交账本。模型只能通过任务专用 submit 工具写入；submission_key
-- 将同一任务快照的重试折叠为一次，防止模拟成交或实盘建议重复执行。
CREATE TABLE IF NOT EXISTS agent_decision_submissions (
    submission_key TEXT PRIMARY KEY,
    task           TEXT NOT NULL,
    mode           TEXT NOT NULL DEFAULT '',
    as_of          TEXT NOT NULL,
    stage          TEXT DEFAULT '',
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('processing','ready','failed')),
    decision       TEXT NOT NULL DEFAULT '{}',
    result         TEXT NOT NULL DEFAULT '{}',
    report         TEXT NOT NULL DEFAULT '',
    error          TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    completed_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_submissions_task_asof
ON agent_decision_submissions(task, mode, as_of, created_at DESC);

-- 消息发送与模型决策解耦。落库成功后先写 outbox，再由独立发送器投递飞书；
-- dedupe_key 保证服务重启或定时任务重试不会重复通知。
CREATE TABLE IF NOT EXISTS agent_message_outbox (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key     TEXT NOT NULL UNIQUE,
    submission_key TEXT NOT NULL,
    channel        TEXT NOT NULL DEFAULT 'feishu',
    message_type   TEXT NOT NULL CHECK (message_type IN ('text','interactive')),
    content        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','sending','sent','failed')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    sent_at        TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (submission_key) REFERENCES agent_decision_submissions(submission_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_outbox_status
ON agent_message_outbox(status, id);

-- 飞书入站消息账本。message_id 是业务幂等键：事件重投、监听器重启或
-- 人工重放都不能让同一条成交回报或 Agent 请求执行两次。
CREATE TABLE IF NOT EXISTS feishu_inbound_messages (
    message_id    TEXT PRIMARY KEY,
    event_id      TEXT NOT NULL DEFAULT '',
    chat_id       TEXT NOT NULL,
    sender_id     TEXT NOT NULL,
    message_type  TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'processing'
                  CHECK (status IN ('processing','succeeded','failed','ignored')),
    handler       TEXT NOT NULL DEFAULT '',
    ack_status    TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    message_at    TEXT NOT NULL DEFAULT '',
    received_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    processed_at  TEXT NOT NULL DEFAULT '',
    replied_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_feishu_inbound_status_received
ON feishu_inbound_messages(status, received_at);

-- 渠道无关的入站消息账本。旧 feishu_inbound_messages 保留用于无损迁移，
-- 新的飞书和企微监听器都只读写此表，并通过 message_id 前缀隔离渠道。
CREATE TABLE IF NOT EXISTS bot_inbound_messages (
    message_id    TEXT PRIMARY KEY,
    event_id      TEXT NOT NULL DEFAULT '',
    chat_id       TEXT NOT NULL,
    sender_id     TEXT NOT NULL,
    message_type  TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'processing'
                  CHECK (status IN ('processing','succeeded','failed','ignored')),
    handler       TEXT NOT NULL DEFAULT '',
    ack_status    TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    message_at    TEXT NOT NULL DEFAULT '',
    received_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    processed_at  TEXT NOT NULL DEFAULT '',
    replied_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bot_inbound_status_received
ON bot_inbound_messages(status, received_at);
""";


def get_schema_statements() -> List[str]:
    """获取建表 SQL 语句列表"""
    stmts = []
    for line in CREATE_TABLES.split(";"):
        line = line.strip()
        if line:
            stmts.append(line + ";")
    return stmts


# ---- 以下表通过脚本动态创建或维护，此处仅作文档 ----
# screen_candidate_* 由 daily_screen.py 暂存；screen_records 与历史由
# execute_stock_selection.py 在 AI 输出校验通过后维护。
