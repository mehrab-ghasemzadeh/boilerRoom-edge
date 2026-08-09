"""Load variables from a .env file into os.environ (stdlib only)."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


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
