# Stateful entry strategy rollback

## Recovery points

- Baseline branch: `stock-workspace-initial`
- Baseline commit: `8a6617ed16d21af259e0e1090b17f3a1040fe01f`
- Trial branch: `experiment/stateful-entry-strategy`
- Scheduler backup:
  `.run/cron_backups/stateful_entry_20260825_215441`

The scheduler backup contains the Hermes `stock` profile `jobs.json`, all 12
profile task scripts, the user crontab, `stock-web.service`, and `SHA256SUMS`.
It is an operational local backup and is intentionally not committed because
Hermes profile state may contain delivery metadata.

Verify the backup before using it:

```bash
cd .run/cron_backups/stateful_entry_20260825_215441
sha256sum --check SHA256SUMS
```

## Switch strategies

Use the operational copy that lives under `.run`; it remains available after
Git changes the checked-out branch:

```bash
.run/switch_strategy_branch.sh status
.run/switch_strategy_branch.sh baseline
.run/switch_strategy_branch.sh experiment
```

The switch refuses a dirty workspace.  It briefly stops the stock Hermes
gateway, changes the branch, rebuilds the derived candidate board, restarts the
Web service, and resumes the gateway.  It does not delete or rewrite market
data, simulated orders, simulated positions, live capital flows, live shadow
positions, or live trade intents.

The experiment adds three lifecycle tables.  Returning to the baseline leaves
those tables in place so the trial history can be recovered later; baseline
code does not consume them.
