"""
The half of the keypad that does not touch GPIO: pins, layout, line editing.

Kept separate from ``keypad`` because that module imports ``RPi.GPIO`` at the
top, which does not exist on a development laptop. Everything here can be
imported, tested and exercised anywhere — which is what lets the mock keypad
put a typed line through exactly the key-by-key path the real one uses.

Wiring
------
Eight pins: four rows driven as outputs, four columns read as inputs with the
internal pull-ups on. Idle, every row is held LOW, so a column reads LOW only
while a key bridges it to a row. To find *which* key, one row at a time is
pulled LOW with the other three HIGH.

Which of the eight connector pins are rows and which are columns is a choice,
not a fact: get it backwards and the matrix still scans, it just comes out
transposed. The default matches the labels on the connector — A/B/C/D as rows,
1/2/3/4 as columns.

Layout
------
A key is identified by its position in the matrix, never by what is printed on
the cap, because the print varies between keypads that are wired identically.
The default below is the common 4x4 membrane arrangement. A calculator-style
pad often puts 7/8/9 on the top row instead, so treat the default as a starting
point and confirm it: ``python src/keypad.py --test`` prints the position and
token of each key as you press it, and BOILERROOM_KEYPAD_LAYOUT overrides the
table without touching this file.

Tokens
------
Each position emits either a single character, which is appended to whatever is
being typed, or one of three control tokens:

  ``enter``   accept the line
  ``del``     rub out the last character
  ``cancel``  abandon the line — every menu reads an empty answer as "back"

Ten digits leaves six keys for the six functions the menus actually need:
accept, rub out, cancel, ``.`` for a decimal temperature, ``,`` to separate a
list of units, and ``-`` which is what clears a limit.
"""

from __future__ import annotations

import os

# Connector pin -> BCM GPIO. Chosen to avoid the relay pins (18, 23, 24, 25,
# 12, 16, 20, 21), the 1-Wire bus (4) and the SPI pins the gas ADC uses.
DEFAULT_ROW_GPIO = (17, 27, 22, 5)  # connector A, B, C, D
DEFAULT_COL_GPIO = (6, 13, 19, 26)  # connector 1, 2, 3, 4

# Names the connector uses, for messages during bring-up.
ROW_LABELS = ("A", "B", "C", "D")
COL_LABELS = ("1", "2", "3", "4")

ENTER = "enter"
DEL = "del"
CANCEL = "cancel"
CONTROL_TOKENS = (ENTER, DEL, CANCEL)

# Spellings accepted in BOILERROOM_KEYPAD_LAYOUT. The layout is comma-separated,
# so the comma key has to be named rather than written.
KEY_ALIASES = {
    "dot": ".",
    "point": ".",
    "decimal": ".",
    "comma": ",",
    "minus": "-",
    "dash": "-",
    "hash": "#",
    "star": "*",
    "ok": ENTER,
    "eq": ENTER,
    "equals": ENTER,
    "back": CANCEL,
    "esc": CANCEL,
    "clear": CANCEL,
    "backspace": DEL,
    "delete": DEL,
}

# Row-major, matching DEFAULT_ROW_GPIO x DEFAULT_COL_GPIO.
DEFAULT_LAYOUT = (
    ("1", "2", "3", ENTER),
    ("4", "5", "6", DEL),
    ("7", "8", "9", CANCEL),
    (".", "0", ",", "-"),
)

# An answer longer than this is a stuck key, not an operator. The longest thing
# any prompt legitimately wants is an 8-digit date.
MAX_LINE_LENGTH = 24


class KeypadLayoutError(ValueError):
    """Raised when a configured pin map or layout cannot be used."""


def _pins_from_env(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default

    pins: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            pins.append(int(part))
        except ValueError as exc:
            raise KeypadLayoutError(
                f"{name}: {part!r} is not a BCM pin number"
            ) from exc

    if len(pins) != 4:
        raise KeypadLayoutError(f"{name}: expected 4 pins, got {len(pins)}")
    if len(set(pins)) != 4:
        raise KeypadLayoutError(f"{name}: the same pin is listed twice — {pins}")
    return tuple(pins)


def row_pins() -> tuple[int, ...]:
    return _pins_from_env("BOILERROOM_KEYPAD_ROWS", DEFAULT_ROW_GPIO)


def col_pins() -> tuple[int, ...]:
    return _pins_from_env("BOILERROOM_KEYPAD_COLS", DEFAULT_COL_GPIO)


def normalise_token(raw: str) -> str:
    """One layout cell -> the token that position emits."""
    text = raw.strip()
    if not text:
        raise KeypadLayoutError("a layout cell is empty")

    lowered = text.lower()
    if lowered in CONTROL_TOKENS:
        return lowered
    if lowered in KEY_ALIASES:
        return KEY_ALIASES[lowered]
    if len(text) == 1 and text.isprintable():
        return text

    raise KeypadLayoutError(
        f"unknown key {raw.strip()!r} — use a single character, one of "
        f"{', '.join(CONTROL_TOKENS)}, or a name from "
        f"{', '.join(sorted(KEY_ALIASES))}"
    )


def parse_layout(raw: str) -> tuple[tuple[str, ...], ...]:
    """
    ``"1,2,3,enter/4,5,6,del/..."`` -> a 4x4 grid of tokens.

    Rows are separated by ``/`` (or ``;``), cells within a row by commas.
    """
    rows: list[tuple[str, ...]] = []
    for line in raw.replace(";", "/").split("/"):
        if not line.strip():
            continue
        cells = [normalise_token(cell) for cell in line.split(",")]
        if len(cells) != 4:
            raise KeypadLayoutError(
                f"row {len(rows) + 1} has {len(cells)} key(s), expected 4"
            )
        rows.append(tuple(cells))

    if len(rows) != 4:
        raise KeypadLayoutError(f"expected 4 rows, got {len(rows)}")
    return tuple(rows)


def layout() -> tuple[tuple[str, ...], ...]:
    """The active layout: BOILERROOM_KEYPAD_LAYOUT, or the default table."""
    raw = (os.environ.get("BOILERROOM_KEYPAD_LAYOUT") or "").strip()
    if not raw:
        return DEFAULT_LAYOUT
    return parse_layout(raw)


def producible_characters(grid: tuple[tuple[str, ...], ...] | None = None) -> set[str]:
    """Every character this keypad can put into an answer."""
    return {
        token
        for row in (grid or layout())
        for token in row
        if token not in CONTROL_TOKENS
    }


def describe(grid: tuple[tuple[str, ...], ...] | None = None) -> list[str]:
    """The layout as a picture, for the menu and for bring-up."""
    grid = grid or layout()
    lines = ["      " + "".join(f"{label:^9}" for label in COL_LABELS)]
    for label, row in zip(ROW_LABELS, grid):
        lines.append(f"  {label}   " + "".join(f"{token:^9}" for token in row))
    return lines


class LineEditor:
    """
    Assembles keypresses into one answer.

    The menus were built around ``input()`` and ask for whole lines, so the
    keypad produces whole lines too rather than the menus growing a second,
    key-at-a-time control path that could drift out of step with the first.

    ``cancel`` and an empty ``enter`` both finish with an empty string, which
    every prompt in the menu already treats as "back" — so there is no way to
    get stuck in a prompt with no keyboard to hand.
    """

    def __init__(self, *, max_length: int = MAX_LINE_LENGTH):
        self._max_length = max_length
        self._characters: list[str] = []
        self.cancelled = False

    @property
    def text(self) -> str:
        return "" if self.cancelled else "".join(self._characters)

    def feed(self, token: str) -> bool:
        """Apply one keypress. Returns True once the answer is complete."""
        if token == ENTER:
            return True
        if token == CANCEL:
            self.cancelled = True
            self._characters.clear()
            return True
        if token == DEL:
            if self._characters:
                self._characters.pop()
            return False

        if len(self._characters) < self._max_length:
            self._characters.append(token)
        return False
