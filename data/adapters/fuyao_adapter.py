"""Optional, bounded Fuyao financial indicators; never infer quote freshness."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


class FuyaoAdapter:
    def __init__(self, api_key=None, timeout=12):
        path = Path(os.environ.get("STOCK_FUYAO_CONFIG") or Path(__file__).resolve().parents[2] / "config/fuyao.local.json")
        self.api_key = api_key if api_key is not None else os.environ.get("FUYAO_API_KEY", "")
        if not self.api_key and path.is_file():
            self.api_key = json.loads(path.read_text()).get("api_key", "")
        self.timeout = timeout

    def financials(self, code, report):
        if not self.api_key:
            return None
        if not re.fullmatch(r"\d{6}", code) or not re.fullmatch(r"\d{4}-[1-4]", report):
            raise ValueError("invalid Fuyao code/report")
        from data.fetcher.tencent_quote import _tencent_symbol
        thscode = code + "." + _tencent_symbol(code)[:2].upper()
        query = urllib.parse.urlencode({"thscode": thscode, "report": report})
        req = urllib.request.Request("https://fuyao.aicubes.cn/api/a-share/financials/indicators?" + query,
                                     headers={"X-api-key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not expose response bodies/headers (which can contain credentials).
            raise RuntimeError(f"Fuyao HTTP {exc.code}; no immediate retry") from None
        except Exception:
            raise RuntimeError("Fuyao transport/JSON failure; no immediate retry") from None
        if raw.get("code") != 0:
            raise RuntimeError(f"Fuyao business code {raw.get('code')}; no immediate retry")
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
