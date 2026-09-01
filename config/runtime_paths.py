"""Resolve user-specific configuration paths without hard-coding a workstation."""
from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def configurable_path(env_name: str, default_relative_path: str) -> Path:
    """Return an environment override or a project-relative local default.

    Relative overrides are resolved from the project root so cron, systemd and
    interactive runs behave identically regardless of their working directory.
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return PROJECT_ROOT / default_relative_path
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path
