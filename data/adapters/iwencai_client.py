"""同花顺问财 OpenAPI 通用客户端。

只负责鉴权、请求、节流、重试和原始 JSON 返回；字段解释由上层 adapter/service 完成。
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from data.adapters.iwencai_credentials import IwenCaiKeyring, is_quota_http_error


class IwenCaiClient:
    BASE_URL = "https://openapi.iwencai.com"
    QUERY2DATA_PATH = "/v1/query2data"
    NEWS_SEARCH_PATH = "/v1/comprehensive/search"

    def __init__(self, api_key: str = "", min_interval: float = 0.35, timeout: int = 30):
        self.api_key = api_key or self.load_api_key()
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_call = 0.0

    def query2data(
        self,
        query: str,
        *,
        skill_id: str,
        page: int = 1,
        limit: int = 10,
        is_cache: str = "1",
        expand_index: str = "true",
        call_type: str = "normal",
    ) -> Dict[str, Any]:
        payload = {
            "query": query,
            "page": str(page),
            "limit": str(limit),
            "is_cache": str(is_cache),
            "expand_index": str(expand_index),
        }
        return self._post_json(
            self.QUERY2DATA_PATH,
            payload,
            skill_id=skill_id,
            call_type=call_type,
        )

    def search_news(
        self,
        query: str,
        *,
        skill_id: str = "news-search",
        channels: List[str] | None = None,
        call_type: str = "normal",
    ) -> Dict[str, Any]:
        payload = {
            "channels": channels or ["news"],
            "app_id": "AIME_SKILL",
            "query": query,
        }
        return self._post_json(
            self.NEWS_SEARCH_PATH,
            payload,
            skill_id=skill_id,
            call_type=call_type,
        )

    def _post_json(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        skill_id: str,
        call_type: str = "normal",
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("IWENCAI_API_KEY 未配置")

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        quota_errors: list[str] = []
        for slot, key in IwenCaiKeyring.candidates(self.api_key):
            self.api_key = key
            self._throttle()
            req = urllib.request.Request(self.BASE_URL + path, data=data, method="POST")
            for k, v in self._headers(skill_id=skill_id, call_type=call_type).items():
                req.add_header(k, v)

            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                IwenCaiKeyring.promote(key)
                return result
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if is_quota_http_error(e.code, body):
                    quota_errors.append(f"{slot}: HTTP {e.code} {body[:120]}")
                    continue
                raise RuntimeError(f"问财 API HTTP {e.code}: {body[:300]}") from e
            except Exception as e:
                raise RuntimeError(f"问财 API 请求失败: {e}") from e

        detail = "; ".join(quota_errors) if quota_errors else "无可用 key"
        raise RuntimeError(f"问财 API HTTP 401: 全部已配置 key 额度不可用: {detail}")

    def _headers(self, *, skill_id: str, call_type: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Claw-Call-Type": call_type,
            "X-Claw-Skill-Id": skill_id,
            "X-Claw-Skill-Version": "1.0.0",
            "X-Claw-Plugin-Id": "none",
            "X-Claw-Plugin-Version": "none",
            "X-Claw-Trace-Id": secrets.token_hex(32),
        }

    def _throttle(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    @staticmethod
    def load_api_key() -> str:
        candidates = IwenCaiKeyring.candidates()
        return candidates[0][1] if candidates else ""
