#!/usr/bin/env python3
"""Evidence and one constrained submission tool for final stock selection."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.stock_selection_repository import (  # noqa: E402
    get_candidate_evidence,
    get_selection_overview,
)
from data.agent_submission_service import submit_stock_selection as submit_selection  # noqa: E402


parser = argparse.ArgumentParser(description="Stock-selection agent MCP")
parser.add_argument("--run-dir", required=True)
parser.add_argument("--provider", default="codex-cli")
parser.add_argument("--model", default="gpt-5.6-sol")
args = parser.parse_args()
RUN_DIR = Path(args.run_dir).resolve()
RUN_DIR.mkdir(parents=True, exist_ok=True)


mcp = FastMCP(
    "stock-selection",
    instructions=(
        "Tools for one final stock-selection decision. "
        "Call selection_overview first, then read every required evidence code "
        "with candidate_evidence using the same as_of. Then call submit_stock_selection once "
        "with the complete decision. A final chat response does not publish candidates. "
        "If submission is rejected with can_retry=true, correct it and submit again."
    ),
)


@mcp.tool()
def selection_overview() -> dict:
    """Get the current staged candidate universe and immutable as_of. Call first."""
    return get_selection_overview()


@mcp.tool()
def candidate_evidence(codes: list[str], as_of: str) -> dict:
    """Get detailed database evidence for in-pool codes using overview.as_of."""
    return get_candidate_evidence(codes, as_of)


@mcp.tool()
def submit_stock_selection(as_of: str, decision: dict) -> dict:
    """Validate and atomically publish the complete final candidate decision."""
    return submit_selection(
        as_of=as_of, decision=decision, run_dir=RUN_DIR,
        provider=args.provider, model=args.model,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
