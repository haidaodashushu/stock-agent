#!/usr/bin/env python3
"""Render and install the per-user Feishu listener service."""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy/systemd/stock-feishu-listener.service.in"
DESTINATION = Path.home() / ".config/systemd/user/stock-feishu-listener.service"


def render() -> str:
    value = TEMPLATE.read_text(encoding="utf-8").replace("{{STOCK_ROOT}}", str(ROOT))
    if "{{" in value or "}}" in value:
        raise RuntimeError(f"unresolved placeholder in {TEMPLATE}")
    return value


def install(*, start: bool = True) -> Path:
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{DESTINATION.name}.", dir=str(DESTINATION.parent), text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(render())
        os.chmod(temporary, 0o644)
        os.replace(temporary, DESTINATION)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if start:
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", DESTINATION.stem], check=True,
        )
    return DESTINATION


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-start", action="store_true", help="install and reload without enabling the service",
    )
    parser.add_argument("--print", action="store_true", dest="print_only")
    args = parser.parse_args()
    if args.print_only:
        print(render(), end="")
        return 0
    print(install(start=not args.no_start))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
