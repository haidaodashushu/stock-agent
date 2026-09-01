"""IwenCai credential loading and quota-failover helpers."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Tuple


KEYRING_PATH = Path(
    os.environ.get(
        "IWENCAI_KEYRING_PATH",
        "~/.config/stock/iwencai_api_keys",
    )
).expanduser()
ZSHRC_PATH = Path(os.environ.get("IWENCAI_ZSHRC_PATH", "~/.zshrc")).expanduser()


class IwenCaiKeyring:
    """Load multiple IwenCai keys and promote the one that still has quota."""

    @staticmethod
    def load_profile_key() -> str:
        key = os.environ.get("IWENCAI_API_KEY", "")
        if key:
            return key.strip()
        for rc in (".zshrc", ".bashrc", ".bash_profile", ".profile"):
            path = Path.home() / rc
            if not path.is_file():
                continue
            try:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if "IWENCAI_API_KEY" not in line or "export" not in line or "=" not in line:
                        continue
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                continue
        return ""

    @classmethod
    def candidates(cls, preferred: str = "") -> List[Tuple[str, str]]:
        seen: set[str] = set()
        result: List[Tuple[str, str]] = []

        def add(slot: str, key: str):
            key = (key or "").strip().strip('"').strip("'")
            if not key or key in seen:
                return
            seen.add(key)
            result.append((slot, key))

        add("active", preferred or cls.load_profile_key())
        for slot, key in cls._read_keyring().items():
            if slot.startswith("IWENCAI_API_KEY_"):
                add(slot, key)
        return result

    @classmethod
    def promote(cls, key: str):
        """Make key the active current key for future cron processes."""
        key = (key or "").strip()
        if not key:
            return

        data = cls._read_keyring()
        current = data.get("IWENCAI_API_KEY_CURRENT", "")
        if key != current:
            data["IWENCAI_API_KEY_FALLBACK_OLD"] = current or data.get("IWENCAI_API_KEY_FALLBACK_OLD", "")
            data["IWENCAI_API_KEY_CURRENT"] = key
        data["ACTIVE"] = "current"
        cls._write_keyring(data)
        cls._write_profile_key(key)

    @staticmethod
    def _read_keyring() -> dict[str, str]:
        if not KEYRING_PATH.is_file():
            return {}
        data: dict[str, str] = {}
        try:
            for line in KEYRING_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip()
        except Exception:
            return {}
        return data

    @staticmethod
    def _write_keyring(data: dict[str, str]):
        KEYRING_PATH.parent.mkdir(parents=True, exist_ok=True)
        ordered = ["ACTIVE", "IWENCAI_API_KEY_CURRENT", "IWENCAI_API_KEY_FALLBACK_OLD"]
        lines = [
            "# IwenCai API keyring for stock workspace. Do not commit or print.",
            "# ACTIVE points to the key currently copied into ~/.zshrc.",
        ]
        for key in ordered:
            value = data.get(key, "")
            if value:
                lines.append(f"{key}={value}")
        for key, value in sorted(data.items()):
            if key not in ordered and value:
                lines.append(f"{key}={value}")
        KEYRING_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            KEYRING_PATH.chmod(0o600)
        except Exception:
            pass

    @staticmethod
    def _write_profile_key(key: str):
        lines: list[str] = []
        replaced = False
        if ZSHRC_PATH.exists():
            lines = ZSHRC_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
        out: list[str] = []
        for line in lines:
            if line.strip().startswith("export IWENCAI_API_KEY="):
                out.append(f"export IWENCAI_API_KEY={key}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"export IWENCAI_API_KEY={key}")
        ZSHRC_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def is_quota_http_error(status: int, body: str) -> bool:
    text = body or ""
    return status == 401 and ("次数已用完" in text or "quota" in text.lower() or "额度" in text)
