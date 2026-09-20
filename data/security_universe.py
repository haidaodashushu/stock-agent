"""Shared deterministic security-universe rules.

Account-specific policies may block additional boards, but the default system
universe must not be copied across screeners, ledgers, and monitoring jobs.
"""
from __future__ import annotations

from collections.abc import Iterable


DEFAULT_BLOCKED_BOARD_PREFIXES = ("688", "8", "4", "920")


def is_supported_board_code(
    code: str, blocked_prefixes: Iterable[str] = DEFAULT_BLOCKED_BOARD_PREFIXES,
) -> bool:
    normalized = str(code or "").zfill(6)
    prefixes = tuple(str(prefix) for prefix in blocked_prefixes)
    return not normalized.startswith(prefixes)
