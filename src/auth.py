"""
Authentication against the Smart Boiler Room API.

Startup: synchronous login stores access + refresh tokens in memory.

Per request: send only the access token. Before each API call, refresh if the
access token is expired (or near expiry). On HTTP 401, refresh once and retry.
The refresh token is never sent on normal requests — only to /auth/refresh.

Full re-login happens only when refresh fails (e.g. refresh token expired).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from load_env import load_dotenv

load_dotenv()

API_BASE_URL = os.environ.get("BOILERROOM_API_BASE_URL", "https://br.mayanext.com").rstrip("/")
WS_BASE_URL = os.environ.get("BOILERROOM_WS_BASE_URL", "wss://br.mayanext.com").rstrip("/")
DEVICE_SERIAL = os.environ.get("BOILERROOM_DEVICE_SERIAL", "")
DEVICE_PASSWORD = os.environ.get("BOILERROOM_DEVICE_PASSWORD", "")
DEVICE_ID = os.environ.get("BOILERROOM_DEVICE_ID", "")
DEVICE_ACCESS_TOKEN = os.environ.get("BOILERROOM_DEVICE_ACCESS_TOKEN", "")

# Refresh access token this many seconds before JWT exp
ACCESS_EXPIRY_BUFFER_SECONDS = int(
    os.environ.get("BOILERROOM_ACCESS_EXPIRY_BUFFER", "30")
)

LOGIN_PATH = "/api/v1/auth/login"
REFRESH_PATH = "/api/v1/auth/refresh"


class AuthError(Exception):
    pass


@dataclass
class TokenPair:
    access: str
    refresh: str


def _unwrap_api_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Unwrap {data, meta} envelope returned by br.mayanext.com API."""
    inner = data.get("data")
    if isinstance(inner, dict):
        return inner
    return data


def _extract_tokens(data: dict[str, Any]) -> TokenPair:
    payload = _unwrap_api_payload(data)
    access = payload.get("access") or payload.get("access_token")
    refresh = payload.get("refresh") or payload.get("refresh_token")
    if not access or not refresh:
        raise AuthError(
            f"Token fields missing in API response. "
            f"Top-level keys: {list(data.keys())}, "
            f"payload keys: {list(payload.keys())}"
        )
    return TokenPair(access=access, refresh=refresh)


def _jwt_expires_at(token: str) -> int | None:
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return int(payload["exp"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _access_token_expired(access: str, buffer_seconds: int = ACCESS_EXPIRY_BUFFER_SECONDS) -> bool:
    expires_at = _jwt_expires_at(access)
    if expires_at is None:
        return True
    return time.time() >= expires_at - buffer_seconds


def _post_json(
    path: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{API_BASE_URL}{path}"
    payload = json.dumps(body).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url,
        data=payload,
        headers=request_headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AuthError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AuthError(f"Could not reach {url}: {exc.reason}") from exc


def _request_json(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{API_BASE_URL}{path}"
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AuthError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AuthError(f"Could not reach {url}: {exc.reason}") from exc


class TokenManager:
    def __init__(self):
        self._tokens: TokenPair | None = None
        self._lock = threading.Lock()

    @property
    def is_authenticated(self) -> bool:
        return self._tokens is not None

    def _require_credentials(self) -> None:
        if not DEVICE_SERIAL or not DEVICE_PASSWORD:
            raise AuthError(
                "BOILERROOM_DEVICE_SERIAL and BOILERROOM_DEVICE_PASSWORD must be set"
            )

    def _login_unlocked(self) -> TokenPair:
        self._require_credentials()
        data = _post_json(LOGIN_PATH, {
            "email": DEVICE_SERIAL,
            "password": DEVICE_PASSWORD,
        })
        self._tokens = _extract_tokens(data)
        return self._tokens

    def _refresh_unlocked(self) -> TokenPair:
        if self._tokens is None:
            raise AuthError("Not authenticated. Call login_sync() first.")

        try:
            data = _post_json(REFRESH_PATH, {"refresh": self._tokens.refresh})
            self._tokens = _extract_tokens(data)
        except AuthError:
            print("[auth] Refresh token rejected; performing full re-login")
            self._login_unlocked()
        return self._tokens

    def login_sync(self) -> TokenPair:
        """Authenticate at startup. Blocks until complete."""
        with self._lock:
            return self._login_unlocked()

    def ensure_valid_access_sync(self) -> str:
        """Return a valid access token, refreshing if expired."""
        with self._lock:
            if self._tokens is None:
                raise AuthError("Not authenticated. Call login_sync() first.")
            if _access_token_expired(self._tokens.access):
                self._refresh_unlocked()
            return self._tokens.access

    async def ensure_valid_access(self) -> str:
        return await asyncio.to_thread(self.ensure_valid_access_sync)

    def authorization_header_sync(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.ensure_valid_access_sync()}"}

    async def authorization_header(self) -> dict[str, str]:
        token = await self.ensure_valid_access()
        return {"Authorization": f"Bearer {token}"}

    def request_json_sync(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Authenticated API request. Refreshes before call if access is expired;
        on 401, refreshes once and retries.
        """
        headers = self.authorization_header_sync()
        try:
            return _request_json(method, path, body, headers=headers)
        except AuthError as exc:
            if "HTTP 401" not in str(exc):
                raise
            with self._lock:
                self._refresh_unlocked()
                headers = {"Authorization": f"Bearer {self._tokens.access}"}
            return _request_json(method, path, body, headers=headers)

    async def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.request_json_sync, method, path, body)

    def device_authorization_header_sync(self) -> dict[str, str]:
        if not DEVICE_ACCESS_TOKEN:
            raise AuthError(
                "BOILERROOM_DEVICE_ACCESS_TOKEN must be set for device API endpoints "
                "(telemetry, WebSocket). Obtain it from POST /api/v1/devices/provision."
            )
        return {"Authorization": f"Device {DEVICE_ACCESS_TOKEN}"}

    def request_json_device_sync(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self.device_authorization_header_sync()
        return _request_json(method, path, body, headers=headers)

    async def request_json_device(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.request_json_device_sync, method, path, body
        )


token_manager = TokenManager()


def login_sync() -> TokenPair:
    """Module-level helper for startup login before asyncio.run()."""
    tokens = token_manager.login_sync()
    print(f"[auth] Logged in as {DEVICE_SERIAL}")
    return tokens
