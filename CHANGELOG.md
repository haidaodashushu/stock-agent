# Changelog

## Unreleased

- Removed the retired OpenClaw cron export and executor.
- Removed legacy daily-trade, point-in-time monitor, and ad-hoc test scripts now
  covered by the unified Hermes trading cycle and the `tests/` suite.
- Updated runtime documentation to describe the active Hermes entrypoints.
- Consolidated three legacy scanners into `daily_screen.py`, stock metadata
  bootstrap/verification into `sync_baostock_basic.py`, and full-history loading
  into `nightly_update.py --days`.
- Collapsed the two-layer nightly Shell runner into `hermes_nightly_update.sh`.
- Removed dormant position/trade/risk plugins and the redundant standalone MA
  cross and volume-breakout selectors.
- Simplified screening to one composite scoring strategy and removed the
  unused strategy registry and its misleading database configuration table.
- Rebuilt the half-hour decision boundary around database-backed current state:
  refresh holdings/candidates, persist current facts, let AI read only the
  database projection plus the unified strategy, then run guarded execution.
- Removed the layered intraday/live snapshot scripts and log-snapshot deltas.
- Removed the top-eight read cap from the half-hour candidate universe; AI now
  receives every candidate persisted for the current trading day.

## 0.1.0 - 2026-06-29

- Initial versioned snapshot of the stock analysis platform.
- Includes source code, strategy modules, web UI, tests, documentation, and OpenClaw cron definitions.
- Excludes runtime databases, logs, packaged archives, local OpenClaw state, and secrets.
