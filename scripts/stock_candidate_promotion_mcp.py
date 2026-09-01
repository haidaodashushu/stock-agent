#!/usr/bin/env python3
"""Evidence and one constrained submission tool for candidate promotion."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.candidate_promotion import (  # noqa: E402
    get_promotion_evidence,
    get_promotion_overview,
)
from data.agent_submission_service import submit_candidate_promotion as submit_promotion  # noqa: E402


parser = argparse.ArgumentParser(description="Candidate-promotion agent MCP")
parser.add_argument("--run-dir")
parser.add_argument("--provider", default="codex-cli")
parser.add_argument("--model", default="gpt-5.6-sol")
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
RUN_DIR = Path(args.run_dir).resolve() if args.run_dir else None
if RUN_DIR:
    RUN_DIR.mkdir(parents=True, exist_ok=True)


mcp = FastMCP(
    "stock-candidate-promotion",
    instructions=(
        "Tools for qualifying opening-auction and intraday-radar observations. "
        "Call promotion_overview first and pass its as_of unchanged to promotion_evidence. "
        "Read every required code, then call submit_candidate_promotion exactly once. "
        "A final chat response does not promote a stock. If rejected with can_retry=true, "
        "correct and resubmit. This server exposes no account and cannot create trades."
    ),
)


@mcp.tool()
def promotion_overview() -> dict:
    """Get the complete current dynamic-promotion scope and immutable version."""
    return get_promotion_overview()


@mcp.tool()
def promotion_evidence(codes: list[str], as_of: str) -> dict:
    """Get complete source evidence for in-scope dynamic stocks."""
    return get_promotion_evidence(codes, as_of)


@mcp.tool()
def submit_candidate_promotion(as_of: str, decision: dict) -> dict:
    """Validate and persist candidate qualification; never creates a trade."""
    if RUN_DIR is None:
        return {"status": "rejected", "reason": "MCP server has no run directory"}
    return submit_promotion(
        as_of=as_of, decision=decision,
        provider=args.provider, model=args.model,
    )


if __name__ == "__main__":
    if args.check:
        overview = get_promotion_overview()
        print(json.dumps({
            "status": "ok",
            "as_of": overview["as_of"],
            "candidate_count": overview["candidate_count"],
        }, ensure_ascii=False))
    else:
        mcp.run(transport="stdio")
