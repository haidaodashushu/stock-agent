#!/usr/bin/env python3
"""Account-scoped evidence and one constrained submission tool for trading."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.trading_decision_repository import (  # noqa: E402
    get_recent_trading_activity,
    get_stock_evidence,
    get_trading_overview,
)
from data.agent_submission_service import submit_trading_decision as submit_decision  # noqa: E402


parser = argparse.ArgumentParser(description="Account-isolated stock trading MCP")
parser.add_argument("--mode", required=True, choices=("simulated", "live"))
parser.add_argument("--stage", required=True)
parser.add_argument("--run-dir", required=True)
parser.add_argument("--provider", default="codex-cli")
parser.add_argument("--model", default="gpt-5.6-sol")
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()
MODE = args.mode
STAGE = args.stage
RUN_DIR = Path(args.run_dir).resolve()
RUN_DIR.mkdir(parents=True, exist_ok=True)


mcp = FastMCP(
    f"stock-{MODE}-trading",
    instructions=(
        f"Tools for one {MODE} stock trading decision. "
        "Call trading_overview first, then pass its as_of unchanged to every follow-up tool. "
        "Read evidence for every required_evidence_code, then call submit_trading_decision once "
        "with a complete decision. A final chat response does not execute anything. "
        "If submission is rejected with can_retry=true, correct the decision and submit again. "
        "This server never exposes the other account or arbitrary database access."
    ),
)


@mcp.tool()
def trading_overview() -> dict:
    """Get this account, market, and complete in-scope universe. Call first."""
    return get_trading_overview(MODE)


@mcp.tool()
def stock_evidence(codes: list[str], as_of: str) -> dict:
    """Get account-isolated evidence for in-scope codes using trading_overview.as_of."""
    return get_stock_evidence(codes, as_of, MODE)


@mcp.tool()
def recent_trading_activity(as_of: str, limit: int = 10) -> dict:
    """Get only this account's recent activity for the same as_of version."""
    return get_recent_trading_activity(as_of, MODE, limit)


@mcp.tool()
def submit_trading_decision(as_of: str, decision: dict) -> dict:
    """Validate and submit this snapshot's complete decision for guarded execution."""
    return submit_decision(
        mode=MODE, stage=STAGE, as_of=as_of, decision=decision,
        run_dir=RUN_DIR, provider=args.provider, model=args.model,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
