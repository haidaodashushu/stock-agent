"""Optional, bounded Fuyao financial indicators; never infer quote freshness."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class FuyaoAdapter:
    def __init__(self, api_key=None, timeout=12, *, api_keys=None, state_path=None):
        path = Path(os.environ.get("STOCK_FUYAO_CONFIG") or Path(__file__).resolve().parents[2] / "config/fuyao.local.json")
        explicit = api_key is not None or api_keys is not None
        keys = api_keys if api_keys is not None else [api_key] if api_key is not None else []
        if not explicit:
            keys = [os.environ.get("FUYAO_API_KEY", "")]
            if not any(keys) and path.is_file():
                config = json.loads(path.read_text())
                keys = [config.get("api_key", ""), *config.get("api_keys", [])]
        self.api_keys = list(dict.fromkeys(k.strip() for k in keys if isinstance(k, str) and k.strip()))
        self.api_key = self.api_keys[0] if self.api_keys else ""  # Legacy compatibility.
        self.timeout = timeout
        self.state_path = Path(state_path) if state_path else (None if explicit else
            Path(os.environ.get("STOCK_FUYAO_KEY_STATE") or path.parent.parent / ".run/fuyao-key-state.json"))
        self._state = {}

    @contextmanager
    def _key_state(self):
        if self.state_path is None:
            yield self._state
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        # Shared by both account workers; no key, response body or header is
        # persisted. Serialize the bounded requests so cooldown is respected.
        fd = os.open(self.state_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                state = json.load(handle)
                if not isinstance(state, dict):
                    state = {}
            except (ValueError, TypeError):
                state = {}
            try:
                yield state
            finally:
                handle.seek(0)
                json.dump(state, handle)
                handle.truncate()
                handle.flush()

    @staticmethod
    def _retry_delay(headers):
        value = (headers or {}).get("Retry-After", "")
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                seconds = (parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 60
        return max(60, seconds) if math.isfinite(seconds) else 60

    def _request(self, url):
        with self._key_state() as state:
            cooldowns = state.setdefault("cooldowns", {})
            keys = [(hashlib.sha256(k.encode()).hexdigest(), k) for k in self.api_keys]
            keys.sort(key=lambda pair: pair[0] != state.get("preferred"))
            for key_id, key in keys:
                if cooldowns.get(key_id, 0) > time.time():
                    continue
                req = urllib.request.Request(url, headers={"X-api-key": key})
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as response:
                        raw = json.load(response)
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        cooldowns[key_id] = time.time() + self._retry_delay(exc.headers)
                        continue  # At most one attempt per configured key.
                    raise RuntimeError(f"Fuyao HTTP {exc.code}; no immediate retry") from None
                except Exception:
                    raise RuntimeError("Fuyao transport/JSON failure; no immediate retry") from None
                if not isinstance(raw, dict) or raw.get("code") != 0:
                    code = raw.get("code") if isinstance(raw, dict) else None
                    # Only known HTTP 429 is classified as rate limiting.
                    safe_code = code if isinstance(code, int) else "unknown"
                    raise RuntimeError(f"Fuyao business code {safe_code}; no immediate retry")
                state["preferred"] = key_id
                cooldowns.pop(key_id, None)
                return raw
            raise RuntimeError("Fuyao all configured keys rate limited or cooling down; deferred retry")

    def financials(self, code, report):
        if not self.api_key:
            return None
        if not re.fullmatch(r"\d{6}", code) or not re.fullmatch(r"\d{4}-[1-4]", report):
            raise ValueError("invalid Fuyao code/report")
        from data.fetcher.tencent_quote import _tencent_symbol
        thscode = code + "." + _tencent_symbol(code)[:2].upper()
        query = urllib.parse.urlencode({"thscode": thscode, "report": report})
        raw = self._request("https://fuyao.aicubes.cn/api/a-share/financials/indicators?" + query)
        data = raw.get("data") or {}
        if data.get("thscode") != thscode or data.get("report") != report:
            raise ValueError("Fuyao returned another stock/report")
        values = {}
        for ability in data.get("abilities", []):
            for item in ability.get("indicators", []):
                value = item.get("value")
                try:
                    value = float(value) if value is not None and not isinstance(value, bool) else None
                    value = value if value is not None and math.isfinite(value) else None
                except (ValueError, TypeError):
                    value = None
                values[str(item.get("index_id") or "")] = value
        return {"source": "fuyao", "report": report, "indicators": values}
