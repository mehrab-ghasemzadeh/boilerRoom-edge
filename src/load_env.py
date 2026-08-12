"""Load variables from a .env file into os.environ (stdlib only)."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

# Values needing quotes if they are ever anything other than the digits the
# platform issues.
_NEEDS_QUOTING = set(" \t#'\"")


def env_path(name: str, default: str | Path) -> Path:
    """
    Path from the environment, resolved against the project root when relative.

    systemd starts services from ``/``, so a relative path like
    ``data/readings.db`` would otherwise resolve to ``/data/readings.db``.
    """
    raw = os.environ.get(name) or str(default)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or DEFAULT_ENV_PATH
    if not env_path.exists():
        return

    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _quote(value: str) -> str:
    if any(character in _NEEDS_QUOTING for character in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def update_env_file(values: dict[str, str], path: Path | None = None) -> Path:
    """
    Set keys in the .env file, leaving everything else in it alone.

    Used to record what an operator typed at the panel, so a device is asked
    for its credentials once rather than at every boot. Every other line —
    comments, endpoints, tuning — is copied through untouched, because this
    file is hand-edited and a rewrite that reformatted it would be a nasty
    surprise the next time somebody opened it.

    Written the way the caches are: to a sibling temp file, then renamed into
    place, so a power cut halfway through cannot leave a truncated file. That
    matters more here than for a cache — after this runs, this file holds the
    only copy of the device's credentials.

    Returns the path written.
    """
    target = path or DEFAULT_ENV_PATH
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []

    remaining = dict(values)
    lines: list[str] = []

    for line in existing:
        stripped = line.lstrip()
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not stripped.startswith("#")
            else None
        )
        if key is not None and key in remaining:
            lines.append(f"{key}={_quote(remaining.pop(key))}")
        else:
            lines.append(line)

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Entered on the device and kept here, so it is asked for once.")
        for key, value in remaining.items():
            lines.append(f"{key}={_quote(value)}")

    temp_path = target.with_name(target.name + ".tmp")
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        # Credentials. Readable by their owner only, on hosts where that means
        # anything — the mode is set before the rename so there is no window
        # where the real file is world-readable.
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    os.replace(temp_path, target)

    return target
