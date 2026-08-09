"""
Device authentication against the Smart Boiler Room API (see DEVICE.md).

Startup: ``login_sync()`` exchanges the numeric device username/password for an
opaque session token via ``POST /api/v1/auth/device/login``.

Per request: send ``Authorization: Device <token>``. The same token is used for
REST and for the device WebSocket. Devices have no refresh endpoint — when the
token nears expiry (or the server answers 401) we simply log in again.

Credentials come from .env:
  BOILERROOM_DEVICE_USERNAME   numeric device username (from provisioning)
  BOILERROOM_DEVICE_PASSWORD   numeric device password
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from load_env import load_dotenv
from logging_setup import get_logger

load_dotenv()

_log = get_logger("auth")

API_BASE_URL = os.environ.get("BOILERROOM_API_BASE_URL", "https://br.mayanext.com").rstrip("/")
WS_BASE_URL = os.environ.get("BOILERROOM_WS_BASE_URL", "wss://br.mayanext.com").rstrip("/")

DEVICE_USERNAME = os.environ.get("BOILERROOM_DEVICE_USERNAME", "")
DEVICE_PASSWORD = os.environ.get("BOILERROOM_DEVICE_PASSWORD", "")

# Optional: pin the id used in REST/WS paths (public_id or serial_number).
# When empty, the device_id returned by the login response is used.
DEVICE_ID = os.environ.get("BOILERROOM_DEVICE_ID", "")

# Re-login this many seconds before the session token expires.
TOKEN_EXPIRY_BUFFER_SECONDS = int(
    os.environ.get("BOILERROOM_TOKEN_EXPIRY_BUFFER", "60")
)

# Fallback when the server omits expires_in (DEVICE_SESSION_LIFETIME_SECONDS).
DEFAULT_SESSION_LIFETIME_SECONDS = 86400

DEVICE_LOGIN_PATH = "/api/v1/auth/device/login"


class AuthError(Exception):
    pass


class MissingCredentialsError(AuthError):
    """Credentials are absent — retrying will not help."""


@dataclass
class DeviceSession:
    token: str
    token_type: str
    expires_at: float
    device_id: str

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - TOKEN_EXPIRY_BUFFER_SECONDS


def _unwrap_api_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the {data, meta} envelope returned by the API."""
    inner = data.get("data")
    if isinstance(inner, dict):
        return inner
    return data


def _extract_session(data: dict[str, Any]) -> DeviceSession:
    payload = _unwrap_api_payload(data)
    token = payload.get("token")
    if not token:
        raise AuthError(
            f"No 'token' in device login response. "
            f"Top-level keys: {list(data.keys())}, payload keys: {list(payload.keys())}"
        )

    try:
        expires_in = int(payload.get("expires_in") or DEFAULT_SESSION_LIFETIME_SECONDS)
    except (TypeError, ValueError):
        expires_in = DEFAULT_SESSION_LIFETIME_SECONDS

    return DeviceSession(
        token=token,
        token_type=payload.get("token_type") or "Device",
        expires_at=time.time() + expires_in,
        device_id=payload.get("device_id") or "",
    )


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
            raw = response.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AuthError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AuthError(f"Could not reach {url}: {exc.reason}") from exc


class DeviceTokenManager:
    def __init__(self):
        self._session: DeviceSession | None = None
        self._lock = threading.Lock()

    @property
    def is_authenticated(self) -> bool:
        return self._session is not None

    @property
    def session(self) -> DeviceSession | None:
        return self._session

    def _require_credentials(self) -> None:
        if not DEVICE_USERNAME or not DEVICE_PASSWORD:
            raise MissingCredentialsError(
                "BOILERROOM_DEVICE_USERNAME and BOILERROOM_DEVICE_PASSWORD must be set "
                "in .env (obtained when the device was provisioned)"
            )
        if not (DEVICE_USERNAME.isdigit() and DEVICE_PASSWORD.isdigit()):
            _log.warning(
                "DEVICE.md requires digits-only device username/password"
            )

    def _login_unlocked(self) -> DeviceSession:
        self._require_credentials()
        data = _request_json(
            "POST",
            DEVICE_LOGIN_PATH,
            {
                "username": DEVICE_USERNAME,
                "password": DEVICE_PASSWORD,
            },
        )
        self._session = _extract_session(data)
        return self._session

    def login_sync(self) -> DeviceSession:
        """Authenticate at startup. Blocks until complete."""
        with self._lock:
            return self._login_unlocked()

    async def login(self) -> DeviceSession:
        return await asyncio.to_thread(self.login_sync)

    def ensure_valid_token_sync(self) -> str:
        """Return a usable session token, logging in again if it expired."""
        with self._lock:
            if self._session is None or self._session.is_expired:
                self._login_unlocked()
            return self._session.token

    async def ensure_valid_token(self) -> str:
        return await asyncio.to_thread(self.ensure_valid_token_sync)

    def authorization_header_sync(self) -> dict[str, str]:
        return {"Authorization": f"Device {self.ensure_valid_token_sync()}"}

    async def authorization_header(self) -> dict[str, str]:
        token = await self.ensure_valid_token()
        return {"Authorization": f"Device {token}"}

    def device_id_sync(self) -> str:
        """Id used in REST/WS paths: env override, else the login response."""
        if DEVICE_ID:
            return DEVICE_ID
        if self._session is None:
            raise AuthError(
                "Device id is unknown — log in first or set BOILERROOM_DEVICE_ID in .env"
            )
        if not self._session.device_id:
            raise AuthError(
                "Login response contained no device_id — set BOILERROOM_DEVICE_ID in .env"
            )
        return self._session.device_id

    def request_json_sync(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Authenticated device API request. Logs in again before the call if the
        session expired; on HTTP 401, logs in once more and retries.
        """
        headers = self.authorization_header_sync()
        try:
            return _request_json(method, path, body, headers=headers)
        except AuthError as exc:
            if "HTTP 401" not in str(exc):
                raise
            with self._lock:
                self._login_unlocked()
                headers = {"Authorization": f"Device {self._session.token}"}
            return _request_json(method, path, body, headers=headers)

    async def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.request_json_sync, method, path, body)


token_manager = DeviceTokenManager()


def device_id() -> str:
    """Id to use in REST/WS paths for this device."""
    return token_manager.device_id_sync()


def login_sync() -> DeviceSession:
    """Module-level helper for startup login before asyncio.run()."""
    session = token_manager.login_sync()
    _log.info(
        "Device session established for %s (device_id=%s)",
        DEVICE_USERNAME,
        session.device_id or DEVICE_ID or "?",
    )
    return session
