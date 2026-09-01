"""Enterprise WeChat (WeCom) application messaging and callback crypto."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


@dataclass(frozen=True)
class WeComSettings:
    corp_id: str
    agent_id: int
    secret: str
    token: str
    encoding_aes_key: str
    allowed_user_ids: frozenset[str]
    admin_user_ids: frozenset[str]
    allow_all_users: bool = False
    callback_enabled: bool = True
    group_chat_id: str = ""
    agent_enabled: bool = True
    agent_model: str = "gpt-5.6-sol"
    agent_timeout_seconds: int = 600


def load_wecom_settings(path: Path) -> WeComSettings:
    if not path.is_file():
        raise RuntimeError(f"WeCom runtime config does not exist: {path}")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    raw = parsed.get("wecom") if isinstance(parsed, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    agent = parsed.get("agent") if isinstance(parsed, dict) else {}
    agent = agent if isinstance(agent, dict) else {}
    corp_id = os.environ.get("STOCK_WECOM_CORP_ID") or str(raw.get("corp_id") or "")
    secret = os.environ.get("STOCK_WECOM_SECRET") or str(raw.get("secret") or "")
    token = os.environ.get("STOCK_WECOM_TOKEN") or str(raw.get("token") or "")
    aes_key = os.environ.get("STOCK_WECOM_ENCODING_AES_KEY") or str(
        raw.get("encoding_aes_key") or ""
    )
    agent_id = int(os.environ.get("STOCK_WECOM_AGENT_ID") or raw.get("agent_id") or 0)
    env_users = os.environ.get("STOCK_WECOM_ALLOWED_USER_IDS", "")
    users = {
        str(value).strip()
        for value in [*(raw.get("allowed_user_ids") or []), *env_users.split(",")]
        if str(value).strip()
    }
    env_admins = os.environ.get("STOCK_WECOM_ADMIN_USER_IDS", "")
    admins = {
        str(value).strip()
        for value in [*(raw.get("admin_user_ids") or []), *env_admins.split(",")]
        if str(value).strip()
    }
    env_allow_all = os.environ.get("STOCK_WECOM_ALLOW_ALL_USERS", "").strip().lower()
    allow_all_users = (
        env_allow_all in {"1", "true", "yes", "on"}
        if env_allow_all
        else bool(raw.get("allow_all_users", False))
    )
    if not corp_id.startswith("ww") or not secret or agent_id <= 0:
        raise RuntimeError("WeCom corp_id, agent_id or secret is not configured")
    if not token or len(aes_key) != 43:
        raise RuntimeError("WeCom callback token or 43-character encoding_aes_key is not configured")
    if not allow_all_users and not users:
        raise RuntimeError("WeCom requires at least one allowed_user_id")
    if not admins:
        raise RuntimeError("WeCom requires at least one admin_user_id")
    if not allow_all_users and not admins.issubset(users):
        raise RuntimeError("WeCom admin_user_ids must be included in allowed_user_ids")
    return WeComSettings(
        corp_id=corp_id,
        agent_id=agent_id,
        secret=secret,
        token=token,
        encoding_aes_key=aes_key,
        allowed_user_ids=frozenset(users),
        admin_user_ids=frozenset(admins),
        allow_all_users=allow_all_users,
        callback_enabled=bool(raw.get("callback_enabled", True)),
        group_chat_id=str(raw.get("group_chat_id") or "").strip(),
        agent_enabled=bool(raw.get("agent_enabled", True)),
        agent_model=str(agent.get("model") or "gpt-5.6-sol"),
        agent_timeout_seconds=max(30, int(raw.get("agent_timeout_seconds") or 600)),
    )


class WeComCrypto:
    """Verify and decrypt callbacks in WeCom's AES-CBC callback format."""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        if len(encoding_aes_key) != 43:
            raise ValueError("encoding_aes_key must contain 43 characters")
        self.token = token
        self.corp_id = corp_id
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise ValueError("encoding_aes_key is invalid")

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        values = sorted((self.token, str(timestamp), str(nonce), encrypted))
        return hashlib.sha1("".join(values).encode("utf-8")).hexdigest()

    def verify(self, signature: str, timestamp: str, nonce: str, encrypted: str) -> None:
        expected = self.signature(timestamp, nonce, encrypted)
        if not secrets.compare_digest(expected, str(signature or "")):
            raise ValueError("invalid WeCom callback signature")

    def decrypt(self, encrypted: str) -> str:
        ciphertext = base64.b64decode(encrypted)
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        if not padded:
            raise ValueError("empty WeCom callback payload")
        padding = padded[-1]
        if padding < 1 or padding > 32 or padded[-padding:] != bytes([padding]) * padding:
            raise ValueError("invalid WeCom callback padding")
        plain = padded[:-padding]
        if len(plain) < 20:
            raise ValueError("invalid WeCom callback payload")
        length = struct.unpack("!I", plain[16:20])[0]
        message = plain[20:20 + length]
        receive_id = plain[20 + length:].decode("utf-8")
        if receive_id and receive_id != self.corp_id:
            raise ValueError("WeCom callback receiver does not match corp_id")
        return message.decode("utf-8")


class WeComClient:
    API_ROOT = "https://qyapi.weixin.qq.com/cgi-bin"

    def __init__(self, settings: WeComSettings) -> None:
        self.settings = settings
        self._token = ""
        self._token_expiry = 0.0
        self._lock = Lock()

    @staticmethod
    def _json_request(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "stock-agent/1"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if int(result.get("errcode") or 0) != 0:
            raise RuntimeError(
                f"WeCom API error {result.get('errcode')}: {result.get('errmsg') or 'unknown'}"
            )
        return result

    def access_token(self, *, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._token_expiry:
                return self._token
            query = urllib.parse.urlencode({
                "corpid": self.settings.corp_id,
                "corpsecret": self.settings.secret,
            })
            result = self._json_request(f"{self.API_ROOT}/gettoken?{query}")
            self._token = str(result["access_token"])
            self._token_expiry = time.time() + max(60, int(result.get("expires_in") or 7200) - 300)
            return self._token

    def send_markdown(self, user_id: str, content: str) -> dict[str, Any]:
        payload = {
            "touser": user_id,
            "msgtype": "markdown",
            "agentid": self.settings.agent_id,
            "markdown": {"content": content[:18000]},
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        }
        token = self.access_token()
        try:
            return self._json_request(
                f"{self.API_ROOT}/message/send?access_token={urllib.parse.quote(token)}", payload,
            )
        except RuntimeError as exc:
            if "42001" not in str(exc) and "40014" not in str(exc):
                raise
            token = self.access_token(force=True)
            return self._json_request(
                f"{self.API_ROOT}/message/send?access_token={urllib.parse.quote(token)}", payload,
            )

    def create_app_chat(
        self,
        *,
        name: str,
        user_ids: list[str],
        owner: str = "",
        chat_id: str = "",
    ) -> str:
        users = list(dict.fromkeys(value.strip() for value in user_ids if value.strip()))
        if len(users) < 2:
            raise ValueError("a WeCom application chat requires at least two users")
        payload: dict[str, Any] = {"name": name[:50], "userlist": users}
        if owner:
            if owner not in users:
                raise ValueError("WeCom application chat owner must be in user_ids")
            payload["owner"] = owner
        if chat_id:
            payload["chatid"] = chat_id
        token = self.access_token()
        result = self._json_request(
            f"{self.API_ROOT}/appchat/create?access_token={urllib.parse.quote(token)}", payload,
        )
        return str(result.get("chatid") or chat_id)

    def send_app_chat_markdown(self, chat_id: str, content: str) -> dict[str, Any]:
        if not chat_id:
            raise ValueError("WeCom application group_chat_id is not configured")
        token = self.access_token()
        return self._json_request(
            f"{self.API_ROOT}/appchat/send?access_token={urllib.parse.quote(token)}",
            {
                "chatid": chat_id,
                "msgtype": "markdown",
                "markdown": {"content": content[:18000]},
                "safe": 0,
            },
        )

    def update_app_chat(
        self,
        chat_id: str,
        *,
        name: str = "",
        owner: str = "",
        add_user_ids: list[str] | None = None,
        delete_user_ids: list[str] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chatid": chat_id}
        if name:
            payload["name"] = name[:50]
        if owner:
            payload["owner"] = owner
        if add_user_ids:
            payload["add_user_list"] = list(dict.fromkeys(add_user_ids))
        if delete_user_ids:
            payload["del_user_list"] = list(dict.fromkeys(delete_user_ids))
        if len(payload) == 1:
            return
        token = self.access_token()
        self._json_request(
            f"{self.API_ROOT}/appchat/update?access_token={urllib.parse.quote(token)}",
            payload,
        )

    def get_app_chat(self, chat_id: str) -> dict[str, Any]:
        token = self.access_token()
        query = urllib.parse.urlencode({"access_token": token, "chatid": chat_id})
        return self._json_request(f"{self.API_ROOT}/appchat/get?{query}")

    def send_to_allowed_users(self, content: str) -> None:
        if self.settings.group_chat_id:
            self.send_app_chat_markdown(self.settings.group_chat_id, content)
        else:
            self.send_markdown("|".join(sorted(self.settings.allowed_user_ids)), content)
