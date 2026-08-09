"""
Logging configuration.

Two properties drive the design:

  * **Nothing blocks the event loop.** Handlers write to an SD card, which
    blocks. Records therefore go onto a queue, and a ``QueueListener`` thread
    does the actual writing.

  * **Records go to the file, not the terminal.** The console belongs to the
    operator's control menu; event chatter scrolling past the prompt makes it
    unusable. Menu replies are written with ``RuntimeState.echo()``, which
    prints without creating a record, and menu option 10 tails the log. Set
    ``BOILERROOM_LOG_CONSOLE_LEVEL`` to a level name to mirror records to the
    terminal while debugging.

The codebase already tags messages by subsystem (``[ws]``, ``[schedule]``,
``[limits]``). Those tags become logger names, so verbosity can be tuned per
subsystem without touching code:

    BOILERROOM_LOG_LEVEL=INFO
    BOILERROOM_LOG_CONSOLE_LEVEL=WARNING     # quiet console, full file
    BOILERROOM_LOG_LEVELS=ws=DEBUG,telemetry=WARNING
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import queue
import sys
from pathlib import Path

from load_env import env_path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ROOT_NAME = "edge"

LOG_PATH = env_path("BOILERROOM_LOG_FILE", Path("data") / "boilerroom.log")
LOG_LEVEL = os.environ.get("BOILERROOM_LOG_LEVEL", "INFO").upper()

# Logs go to the file, not the terminal: the console is the operator's control
# menu, and event chatter scrolling through it makes the menu unusable. Set
# BOILERROOM_LOG_CONSOLE_LEVEL to a level name (WARNING, INFO, DEBUG) to mirror
# records to the terminal while debugging.
LOG_CONSOLE_LEVEL = os.environ.get("BOILERROOM_LOG_CONSOLE_LEVEL", "OFF").upper()

CONSOLE_OFF_VALUES = {"OFF", "NONE", "DISABLED", "FALSE", "0", ""}

# Rotation keeps log growth bounded: 2 MB x 3 backups = 8 MB worst case.
LOG_MAX_BYTES = int(os.environ.get("BOILERROOM_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUPS = int(os.environ.get("BOILERROOM_LOG_BACKUPS", "3"))

FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-16s %(message)s"

_listener: logging.handlers.QueueListener | None = None


def _level_value(name: str, default: int = logging.INFO) -> int:
    value = logging.getLevelName(name.strip().upper())
    return value if isinstance(value, int) else default


class _ConsoleFormatter(logging.Formatter):
    """Render ``[tag] message``, matching the previous print-based output."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()

        tag = record.name.split(".", 1)[1] if "." in record.name else ""
        prefix = f"[{tag}] " if tag else ""
        if record.levelno >= logging.WARNING:
            prefix = f"{record.levelname} {prefix}"

        text = f"{prefix}{message}"
        if record.exc_info:
            text = f"{text}\n{self.formatException(record.exc_info)}"
        return text


def _apply_per_logger_levels() -> None:
    """Apply BOILERROOM_LOG_LEVELS=ws=DEBUG,telemetry=WARNING."""
    for part in os.environ.get("BOILERROOM_LOG_LEVELS", "").split(","):
        name, separator, level = part.partition("=")
        if not separator or not name.strip():
            continue
        logging.getLogger(f"{ROOT_NAME}.{name.strip()}").setLevel(_level_value(level))


def configure_logging() -> logging.Logger:
    """Install the queue-backed console and file handlers. Idempotent."""
    global _listener

    root = logging.getLogger(ROOT_NAME)
    if _listener is not None:
        return root

    console_enabled = LOG_CONSOLE_LEVEL not in CONSOLE_OFF_VALUES
    console_level = _level_value(LOG_CONSOLE_LEVEL) if console_enabled else logging.CRITICAL
    file_level = _level_value(LOG_LEVEL)

    # Levels are decided by the loggers, not the file handler: the root level is
    # the global floor and a per-logger override may sit above or below it. The
    # file handler then archives everything that survives, so an override like
    # ws=DEBUG actually reaches the log instead of being silently filtered out.
    root.setLevel(min(console_level, file_level) if console_enabled else file_level)
    _apply_per_logger_levels()
    root.propagate = False

    handlers: list[logging.Handler] = []
    console: logging.Handler | None = None
    if console_enabled:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(console_level)
        console.setFormatter(_ConsoleFormatter())
        handlers.append(console)

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_PATH,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.NOTSET)  # loggers decide; see above
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
        handlers.append(file_handler)
    except OSError as exc:
        # A read-only or full SD card must not stop the agent from running, but
        # losing the log silently would be worse than a line on the terminal.
        print(f"WARNING [log] Log file unavailable ({exc}) — logging disabled")

    # Records are queued here and written by the listener thread, so a log call
    # from the event loop never waits on stdout or the SD card.
    record_queue: queue.SimpleQueue = queue.SimpleQueue()
    root.addHandler(logging.handlers.QueueHandler(record_queue))

    _listener = logging.handlers.QueueListener(
        record_queue, *handlers, respect_handler_level=True
    )
    _listener.start()

    return root


def shutdown_logging() -> None:
    """Flush and stop the listener thread."""
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None

    root = logging.getLogger(ROOT_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def get_logger(tag: str | None = None) -> logging.Logger:
    """Logger for a subsystem tag, e.g. ``get_logger("ws")`` -> ``edge.ws``."""
    if not tag:
        return logging.getLogger(ROOT_NAME)
    return logging.getLogger(f"{ROOT_NAME}.{tag}")


def _tail_file_sync(path: Path, limit: int) -> list[str]:
    """Last ``limit`` lines of one file, read backwards from the end.

    The log can be megabytes; reading it whole to show 40 lines would be
    careless on a 512 MB device.
    """
    if limit <= 0 or not path.exists():
        return []

    chunk_size = 8192
    data = b""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            while position > 0 and data.count(b"\n") <= limit:
                step = min(chunk_size, position)
                position -= step
                handle.seek(position)
                data = handle.read(step) + data
    except OSError:
        return []

    return data.decode("utf-8", errors="replace").splitlines()[-limit:]


def _tail_sync(limit: int) -> list[str]:
    """Recent log lines, reaching back into rotated files when needed."""
    candidates = [LOG_PATH] + [
        LOG_PATH.with_name(f"{LOG_PATH.name}.{index}")
        for index in range(1, LOG_BACKUPS + 1)
    ]

    collected: list[str] = []
    for path in candidates:
        remaining = limit - len(collected)
        if remaining <= 0:
            break
        # Older files are prepended, keeping the result in chronological order.
        collected = _tail_file_sync(path, remaining) + collected

    return collected


async def tail_log(limit: int = 40) -> list[str]:
    """Read the tail of the log file without blocking the event loop."""
    return await asyncio.to_thread(_tail_sync, limit)


def split_tag(message: str) -> tuple[str | None, str]:
    """Split a ``"[ws] Connected"`` message into ``("ws", "Connected")``."""
    if message.startswith("[") and "]" in message:
        tag, _, rest = message[1:].partition("]")
        if tag and " " not in tag:
            return tag, rest.lstrip()
    return None, message
