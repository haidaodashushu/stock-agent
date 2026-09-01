# Stock Workspace

This repository contains an A-share analysis, screening and paper/live-shadow
trading workspace. Scheduled AI decisions use the Codex CLI; deterministic data
refresh, validation and execution remain local Python or shell processes.

## Quick Start

### 1. Prerequisites

- Linux or macOS with Git and Python 3.11+
- Network access to the configured public market-data providers
- Optional: Codex CLI for AI decisions and `lark-cli` for Feishu delivery

Clone the repository and create an isolated Python environment:

```bash
git clone https://github.com/haidaodashushu/stock-agent.git
cd stock-agent

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2. Create local configuration

The committed examples contain no account data or credentials. Copy the local
files you want to customize; all `*.local.json` files are ignored by Git.

```bash
cp config/live_manual_account.example.json config/live_manual_account.local.json
cp config/watchlist.example.json config/watchlist.local.json
cp config/strategic_theme_pool.example.json config/strategic_theme_pool.local.json
cp config/reconcile_resolved_issues.example.json config/reconcile_resolved_issues.local.json
```

The application can start without these copies by using built-in defaults and
the small example strategic pool. Run the test suite before loading real data:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

### 3. Start the Web dashboard

```bash
scripts/start_web.sh
```

Open <http://127.0.0.1:8899>. The first start creates an empty local SQLite
database under `data/`; database files and logs are ignored by Git. Override
the bind address, port or database path when needed:

```bash
HOST=0.0.0.0 PORT=8899 STOCK_DB_PATH=/path/to/stock.db scripts/start_web.sh
```

Do not expose the dashboard to an untrusted network without adding an
authentication and TLS layer.

### 4. Populate market data (optional)

The dashboard works with an empty database, but screening requires stock
metadata and daily bars. These commands access external data providers and may
take several minutes on the first run:

```bash
python scripts/sync_baostock_basic.py
python scripts/nightly_update.py --days 500
```

Verify the resulting metadata coverage:

```bash
python scripts/sync_baostock_basic.py --verify-only
```

### 5. Enable AI and Feishu integrations (optional)

AI decisions require an authenticated `codex` CLI. Feishu delivery and inbound
messages additionally require an authenticated `lark-cli` profile named
`stock` (or `STOCK_LARK_PROFILE`), plus a private runtime config:

```bash
cp config/runtime.example.json config/runtime.local.json
# Edit target_id, allowed_sender_ids and receive.enabled before continuing.

codex login status
lark-cli --profile stock auth status
.venv/bin/python scripts/switch_agent_runtime.py preflight
```

Install the per-user Feishu listener only after the preflight succeeds:

```bash
.venv/bin/python scripts/install_feishu_listener_service.py
systemctl --user status stock-feishu-listener.service
```

The live-shadow path never connects to a broker or submits a real order. It
only creates advice records and accepts explicit manual-fill reports.

### 6. Run individual jobs

```bash
# Quantitative staging plus AI final selection
scripts/stock_agent_selection_cycle.sh

# Account-isolated intraday decisions
scripts/stock_agent_trading_cycle.sh simulated
scripts/stock_agent_trading_cycle.sh live

# Deterministic nightly refresh
scripts/stock_scheduled_job.sh nightly-update
```

### Enterprise WeChat self-built application

Set `messaging.provider` to `wecom` in `config/runtime.local.json`, then fill
the `wecom` block from `config/runtime.example.json`. Configure the self-built
application callback as:

```text
https://your-public-host/api/wecom/callback
```

Enter the same callback `token` and `encoding_aes_key` in the Enterprise
WeChat admin console. Add the server's outbound public IP to the application's
trusted IP list. Set `allow_all_users` to `true` to accept messages from every
member in the application's Enterprise WeChat visibility scope; otherwise only
members explicitly listed in `allowed_user_ids` are processed. Members in
`admin_user_ids` may perform write operations; all other members run in a
read-only sandbox and cannot submit live fills. The
public Web live-fill/status endpoints are deliberately disabled, so remote live
updates must come from an authorized WeCom admin. Scheduled outbox delivery follows `messaging.provider`, while the
existing Feishu configuration may remain in the local file for rollback.

Reports stored in `agent_message_outbox` are channel-neutral. The delivery
adapter renders the same report at send time: `wecom_aibot` receives native
Markdown (and may use `messaging.mention_all_on_push`), while `feishu` receives
a schema 2.0 interactive card. Business jobs and Agent submission code must not
contain channel-specific formatting. To switch outbound delivery, change only
`messaging.provider`; credentials for the inactive adapter can stay configured.
Inbound listeners are independent services: enable either
`stock-wecom-aibot.service` or `stock-feishu-listener.service` after its own
preflight, rather than coupling listener lifecycle to scheduled jobs. Both
listeners write the channel-neutral `bot_inbound_messages` ledger using distinct
message ID prefixes.

For an intelligent robot, use the `wecom_aibot` block and run the long-lived
`stock-wecom-aibot.service`. It connects outbound through the official
WebSocket channel, accepts private messages and group mentions, and captures
the first group conversation as the scheduled-report target. After that target
is verified, change `messaging.provider` to `wecom_aibot`; the loopback delivery
bridge listens only on `127.0.0.1`.
Set the legacy `wecom.callback_enabled` to `false` after the intelligent robot
has passed both inbound and proactive-send verification.

Every private user has an isolated persistent Codex session. Group context is
also isolated by group and sender so a writable administrator session is never
shared with a read-only member. Send `/new` in a private chat (or mention the
robot with `/new` in a group) to discard the current logical session and start
fresh on the next message. Non-admin members may ask about any topic, while the
runtime keeps their tool execution read-only and rejects live-fill writes before
the model is invoked. Permission language is returned only when a write request
is actually denied; ordinary answers do not repeat role or access notices.

In groups, each sender keeps a separate Codex session, while recent public
questions and answers that actually reached the robot are supplied as shared
group context. Enterprise WeChat only pushes group interactions that mention
the robot, so unrelated group history is not available. Native quoted messages
are expanded from the callback `quote` field; quoted/current images and mixed
text-image messages are downloaded, decrypted and attached to the Codex turn.
The loopback `/send` bridge accepts either Markdown `content` or a native
`template_card` object for proactive group delivery.

The supplied crontab is a template containing `{{STOCK_ROOT}}`; do not install
it verbatim. Existing scheduler migration and rollback instructions are in
[`docs/codex-agent-runtime.md`](docs/codex-agent-runtime.md).

### 7. Migrate runtime data to another server

Do not copy the complete SQLite database unless you need decades of market
history. The compact exporter keeps every account, order, live-fill, candidate,
observation and runtime-state table, but trims daily bars to:

- the latest 120 trading sessions for the whole market, so the next full-market
  screen still has enough history for the current technical indicators;
- the latest 500 sessions available for current holdings, candidates,
  strategic observations, watchlist stocks and active trading state.

Create a private migration package:

```bash
.venv/bin/python scripts/export_migration_snapshot.py \
  --market-sessions 120 \
  --scope-sessions 500 \
  --include-runtime-config
```

The archive and SHA-256 checksum are written under `dist/`, which is ignored by
Git. The archive contains `data/stock_data.db`, applicable `config/*.local.json`
files, `scope.json`, `manifest.json` and restore instructions. It does not
contain Git history, Codex/lark-cli login state, API tokens or user keyrings.
Treat the archive as private because account data and Feishu identifiers may be
included. Verify the checksum and follow the packaged `MIGRATION_README.md` on
the destination host before enabling scheduled jobs.

Tracked content:

- `account/`, `data/`, `engine/`, `strategy/`, `scripts/`, `web/`: platform source code.
- `config/agent_trading_policy.md` plus the simulated/live prompt files: shared strategy and account-isolated AI contracts.
- `config/*.example.json`: safe templates for user-specific runtime configuration.
- `docs/` and `tests/`: architecture, operations and verification.

Not tracked:

- SQLite databases, logs, packaged exports, virtualenvs, local runtime state,
  personal watchlists/account data, monitoring snapshots and secrets.

Local configuration:

```bash
cp config/runtime.example.json config/runtime.local.json
cp config/live_manual_account.example.json config/live_manual_account.local.json
cp config/watchlist.example.json config/watchlist.local.json
cp config/strategic_theme_pool.example.json config/strategic_theme_pool.local.json
cp config/reconcile_resolved_issues.example.json config/reconcile_resolved_issues.local.json
```

Without a local strategic pool, the application loads the small example pool
so a fresh clone can start and run its test suite. Replace it before relying on
strategic-theme monitoring.

Each path can instead be supplied through `STOCK_RUNTIME_CONFIG`,
`STOCK_LIVE_ACCOUNT_CONFIG`, `STOCK_WATCHLIST_CONFIG`,
`STOCK_STRATEGIC_THEME_POOL_CONFIG` or `STOCK_RECONCILE_ISSUES_CONFIG`.
Feishu recipient IDs can also be supplied through `STOCK_FEISHU_CHAT_ID` or
`STOCK_FEISHU_USER_ID`; API credentials are never stored in tracked files.

Runtime entrypoints:

- Simulated intraday AI trading: `scripts/stock_agent_trading_cycle.sh simulated`
- Live-shadow intraday AI trading: `scripts/stock_agent_trading_cycle.sh live`
- Premarket/night screening and close review: `scripts/system_stock_command_job.sh`
- Nightly data refresh: `scripts/stock_scheduled_job.sh nightly-update`
- Web dashboard: `scripts/start_web.sh`

Strategy architecture:

- Premarket screening has one scoring implementation:
  `strategy/selector/technical_scoring.py`. Moving averages, MACD, KDJ,
  volume, position, themes, fundamentals, and logic changes are factors in
  that model rather than separately registered strategies. The selector first
  builds a deterministic 100-stock pool from uniformly covered daily-bar
  technical factors only. It then batch-refreshes announcements and structured
  financial changes for that pool and applies themes, logic changes, optional
  report analysis, fund flow, sector rotation, corporate-action risk, and theme
  concentration before selecting the final 10 candidates. Optional factors
  therefore cannot influence entry into the technical pool and do affect final
  membership rather than only reordering an already-truncated TOP 10.
- Simulated and live-shadow trading are separate scheduled pipelines with
  independent refreshes, locks, logs, AI calls, validation, and execution.
  `stock-simulated-trading` exposes only the simulated account and
  `stock-live-trading` exposes only the live shadow account. They share strategy
  code and market providers, not account state or actions.
- Each pipeline overwrites only its own `mode` partition in
  `trading_stock_state` and `trading_market_state`. There is no cycle ID or
  unbounded intraday snapshot history.
- Effective simulated runs are 09:30, 10:00, 10:30, 11:00, 13:00, 13:30,
  14:00, and 14:30. Live-shadow runs are offset by two minutes; other matches
  from the broad cron expressions exit silently.
- The generic backtest engine accepts an explicit trade callback. It does not
  load runtime strategy plugins.

Canonical maintenance commands:

- Complete screening (quantitative candidate pool + AI final selection):
  `scripts/stock_agent_selection_cycle.sh`
- Quantitative candidate staging only (does not replace `screen_records`):
  `python scripts/daily_screen.py`
- Intraday interval price/turnover absorption: `python scripts/analyze_intraday_flow.py --help`
  (contract and examples: `docs/intraday_flow_analysis.md`)
- Strategic-theme launch radar: `python scripts/scan_intraday_radar.py --pretty`
  (design and recent limit-up study: `docs/intraday_strategic_radar.md`)
- Opening-auction observation report: `python scripts/report_opening_auction_observations.py --days 5`
  (observation-only data contract: `docs/opening_auction_observation.md`)
- Re-run the point-in-time limit-up setup study:
  `python scripts/analyze_limit_up_setups.py --start YYYY-MM-DD --end YYYY-MM-DD`
- Stock metadata, industries, and themes: `python scripts/sync_baostock_basic.py`
- Metadata health check: `python scripts/sync_baostock_basic.py --verify-only`
- Daily-bar bootstrap: `python scripts/nightly_update.py --days 500`

Versioning:

- Current version is stored in `VERSION`.
- Human-readable changes are recorded in `CHANGELOG.md`.
