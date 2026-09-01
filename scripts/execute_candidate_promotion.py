#!/usr/bin/env python3
"""Validate and persist one account-free candidate-promotion response."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.candidate_promotion import (  # noqa: E402
    apply_promotion_decision,
    record_promotion_failure,
)
from scripts.execute_trading_cycle import extract_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply one intraday candidate promotion")
    parser.add_argument("--expected-as-of", required=True)
    parser.add_argument("--response")
    parser.add_argument("--error")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    response_text = ""
    try:
        if args.error:
            result = record_promotion_failure(args.expected_as_of, args.error)
            Path(args.output).write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            print(f"candidate promotion failed: {args.error}", file=sys.stderr)
            return 1
        if not args.response:
            raise ValueError("--response is required unless --error is provided")
        response_text = Path(args.response).read_text(encoding="utf-8")
        payload = extract_json(response_text)
        result = apply_promotion_decision(payload, args.expected_as_of)
    except Exception as exc:
        try:
            result = record_promotion_failure(
                args.expected_as_of, str(exc), response=response_text,
            )
        except Exception as persist_exc:
            result = {
                "status": "failed", "as_of": args.expected_as_of,
                "error": f"{exc}; failure persistence failed: {persist_exc}",
            }
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(f"candidate promotion failed: {exc}", file=sys.stderr)
        return 1
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
