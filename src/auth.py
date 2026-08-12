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

A device that has neither asks for them at the panel instead, once, and keeps
what it is given — see ``set_credentials`` and ``save_credentials`` below. They
are read through functions rather than frozen into module constants at import
precisely so that can work: the answer changes while the process is running.

Nothing is written to .env until a login has actually succeeded with it. An
installer mistypes a digit eventually, and a device that cached the typo would
be one nobody could fix without a keyboard and a screen — which is the position
this whole feature exists to get out of.
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
from pathlib import Path
from typing import Any

from load_env import load_dotenv, update_env_file
from logging_setup import get_logger

load_dotenv()

_log = get_logger("auth")

API_BASE_URL = os.environ.get("BOILERROOM_API_BASE_URL", "https://br.mayanext.com").rstrip("/")
WS_BASE_URL = os.environ.get("BOILERROOM_WS_BASE_URL", "wss://br.mayanext.com").rstrip("/")

USERNAME_KEY = "BOILERROOM_DEVICE_USERNAME"
PASSWORD_KEY = "BOILERROOM_DEVICE_PASSWORD"

# Long enough for anything the platform issues, short enough that a stuck
# keypad key is refused rather than sent.
CREDENTIAL_MAX_LENGTH = 32

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
    """Credentials are absent — retrying will not help, but asking might."""


class CredentialError(ValueError):
    """Raised when what an operator typed cannot be a device credential."""


# ---------------------------------------------------------------------------
# Where the credentials live
# ---------------------------------------------------------------------------
#
# Read from the environment on every use rather than captured at import: on a
# device that has never been paired they arrive later, typed at the panel, and
# a constant read at import would still be empty when they did.


def device_username() -> str:
    return (os.environ.get(USERNAME_KEY) or "").strip()


def device_password() -> str:
    return (os.environ.get(PASSWORD_KEY) or "").strip()


def credentials_present() -> bool:
    return bool(device_username() and device_password())


def validate_credential(text: str, field: str) -> str:
    """
    Check one credential before it is sent anywhere.

    Digits only, because that is what the platform issues and all the keypad
    can produce — so a non-digit means the layout is wrong or a key is stuck,
    and both are worth refusing rather than sending.
    """
    value = (text or "").strip()
    if not value:
        raise CredentialError(f"the {field} cannot be empty")
    if not value.isdigit():
        raise CredentialError(f"the {field} must be digits only")
    if len(value) > CREDENTIAL_MAX_LENGTH:
        raise CredentialError(
            f"the {field} is longer than {CREDENTIAL_MAX_LENGTH} digits"
        )
    return value


# Whether the credentials now in the environment came from an operator and have
# not been written to .env yet. They are held here until a login proves them.
_unsaved = False


def set_credentials(username: str, password: str) -> None:
    """Adopt credentials typed at the panel, for this run. Not yet saved."""
    global _unsaved

    checked_username = validate_credential(username, "username")
    checked_password = validate_credential(password, "password")

    os.environ[USERNAME_KEY] = checked_username
    os.environ[PASSWORD_KEY] = checked_password
    _unsaved = True


def credentials_unsaved() -> bool:
    return _unsaved


def forget_credentials() -> None:
    """
    Drop credentials the server refused, so they can be asked for again.

    Only ever called for unsaved ones. Credentials already in .env are the
    operator's file and this does not get to delete them — a server that starts
    refusing a paired device is a different problem, and silently wiping the
    only copy would make it much worse.
    """
    global _unsaved

    os.environ.pop(USERNAME_KEY, None)
    os.environ.pop(PASSWORD_KEY, None)
    _unsaved = False


def credentials_rejected(exc: Exception) -> bool:
    """
    Whether a failed login means "wrong credentials" rather than "no server".

    The difference decides whether to ask the operator again or to keep
    retrying quietly — asking again because the network is down would be
    useless, and retrying a typo forever is how a device ends up permanently
    offline with nobody being told why.
    """
    text = str(exc)
    return any(f"HTTP {code}" in text for code in (400, 401, 403))


async def save_credentials() -> Path | None:
    """
    Write the credentials to .env, so the next boot does not ask.

    Called only after a login has succeeded with them. Returns the path
    written, or None if there was nothing to save.
    """
    global _unsaved

    if not _unsaved:
        return None

    path = await asyncio.to_thread(
        update_env_file,
        {USERNAME_KEY: device_username(), PASSWORD_KEY: device_password()},
    )
    _unsaved = False
    return path


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
        if not credentials_present():
            raise MissingCredentialsError(
                "This device has no credentials — enter the username and password "
                "it was provisioned with, or put them in .env as "
                "BOILERROOM_DEVICE_USERNAME and BOILERROOM_DEVICE_PASSWORD"
            )
        if not (device_username().isdigit() and device_password().isdigit()):
            # Only reachable from .env: what the panel accepts is checked as it
            # is typed.
            _log.warning(
                "DEVICE.md requires digits-only device username/password"
            )

    def _login_unlocked(self) -> DeviceSession:
        self._require_credentials()
        data = _request_json(
            "POST",
            DEVICE_LOGIN_PATH,
            {
                "username": device_username(),
                "password": device_password(),
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
        device_username(),
        session.device_id or DEVICE_ID or "?",
    )
    return session
