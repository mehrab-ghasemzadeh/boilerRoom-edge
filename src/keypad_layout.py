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

  ``enter``   accept the line — and, on the display, select the highlighted row
  ``del``     rub out the last character
  ``cancel``  go back: every menu reads an empty answer as "back"

Ten digits leaves six keys for the six functions the menus actually need:
accept, rub out, cancel, ``.`` for a decimal temperature, ``,`` to separate a
list of units, and ``-`` which is what clears a limit.

The default below matches the pad fitted to this device, whose caps read::

    1 2 3 A
    4 5 6 B
    7 8 9 C
    * 0 # D

so ``#`` accepts and ``*`` goes back — the two the operator reaches for most,
on the two keys that are not digits and not tucked up the right-hand side. That
leaves A-D for the four remaining functions. On the graphical display, ``2``
and ``8`` also scroll: they sit above and below ``5`` and read as up and down
without anything printed on them saying so.

Caps
----
What is printed on a key and what it emits are different things, and the legend
strip on the display has to name the printed one — telling an operator to press
"cancel" is no use when the cap says ``*``. ``KEY_CAPS`` is that second table,
overridable with BOILERROOM_KEYPAD_CAPS in the same format as the layout.
"""

from __future__ import annotations

import os

from logging_setup import get_logger

_log = get_logger("keypad")

# Connector pin -> BCM GPIO. Chosen to avoid the relay pins (18, 23, 24, 25,
# 12, 16, 20, 21), the 1-Wire bus (4) and the SPI pins the gas ADC uses.
DEFAULT_ROW_GPIO = (11, 13, 15, 29)  # connector 1, 2, 3, 4
DEFAULT_COL_GPIO = (31, 33, 35, 37)  # connector 5, 6, 7, 8

# Names the connector uses, for messages during bring-up.
ROW_LABELS = ("A", "B", "C", "D")
COL_LABELS = ("1", "2", "3", "4")

ENTER = "enter"
DEL = "del"
CANCEL = "cancel"
CONTROL_TOKENS = (ENTER, DEL, CANCEL)

# Spellings accepted in BOILERROOM_KEYPAD_LAYOUT. The layout is comma-separated,
# so the comma key has to be named rather than written.
#
# "#" and "*" are the caps this device's accept and back keys carry, and no
# prompt anywhere wants either as a character — so they are read as the
# functions rather than left as two dead keys for whoever writes a layout the
# obvious way.
KEY_ALIASES = {
    "dot": ".",
    "point": ".",
    "decimal": ".",
    "comma": ",",
    "minus": "-",
    "dash": "-",
    "hash": ENTER,
    "#": ENTER,
    "star": CANCEL,
    "*": CANCEL,
    "ok": ENTER,
    "eq": ENTER,
    "equals": ENTER,
    "select": ENTER,
    "back": CANCEL,
    "esc": CANCEL,
    "clear": CANCEL,
    "backspace": DEL,
    "delete": DEL,
}

# Row-major, matching DEFAULT_ROW_GPIO x DEFAULT_COL_GPIO. See the module
# docstring for why each key does what it does.
DEFAULT_LAYOUT = (
    ("1", "2", "3", DEL),
    ("4", "5", "6", "."),
    ("7", "8", "9", ","),
    (CANCEL, "0", ENTER, "-"),
)

# What is printed on each cap, in the same positions. Only ever used to talk to
# the operator — nothing is decided from it.
DEFAULT_CAPS = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)

# Where the display's scroll keys live. Digits like any other, until a screen
# that scrolls is in front of them.
SCROLL_UP = "2"
SCROLL_DOWN = "8"

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


def parse_caps(raw: str) -> tuple[tuple[str, ...], ...]:
    """Same grid format as the layout, but the cells are labels, not tokens."""
    rows: list[tuple[str, ...]] = []
    for line in raw.replace(";", "/").split("/"):
        if not line.strip():
            continue
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != 4:
            raise KeypadLayoutError(
                f"caps row {len(rows) + 1} has {len(cells)} key(s), expected 4"
            )
        if not all(cells):
            raise KeypadLayoutError(f"caps row {len(rows) + 1} has an empty cap")
        rows.append(tuple(cells))

    if len(rows) != 4:
        raise KeypadLayoutError(f"expected 4 rows of caps, got {len(rows)}")
    return tuple(rows)


def caps() -> tuple[tuple[str, ...], ...]:
    """What is printed on the keys: BOILERROOM_KEYPAD_CAPS, or the default."""
    raw = (os.environ.get("BOILERROOM_KEYPAD_CAPS") or "").strip()
    if not raw:
        return DEFAULT_CAPS
    return parse_caps(raw)


_warned_about_tables = False


def key_tables() -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]:
    """
    The layout and the caps, as far as they can be read. **Never raises.**

    Everything that talks *about* the keypad goes through here rather than
    calling ``layout()`` and ``caps()`` directly: the legend on the display,
    the layout picture in the menu, and the bench keyboard's key mapping. All
    of those run before anything has had a chance to validate a thing — the
    display's legend is built when ``control_menu`` is imported — and a typo in
    an environment variable must not be able to stop a heating agent from
    starting.

    Where the configuration being broken actually matters, it is still refused
    outright: ``Keypad.__init__`` calls ``layout()`` and will not scan a table
    it cannot read. That is the right place for it, because there the failure
    means "this keypad cannot be used", and the menu falls back to the
    keyboard. Here it only means "the legend may name the wrong key".
    """
    global _warned_about_tables

    try:
        return layout(), caps()
    except KeypadLayoutError as exc:
        if not _warned_about_tables:
            _warned_about_tables = True
            _log.warning(
                "Using the default key table for on-screen labels — %s. "
                "The keypad itself will refuse to start until this is fixed.",
                exc,
            )
        return DEFAULT_LAYOUT, DEFAULT_CAPS


def cap_for(
    token: str,
    grid: tuple[tuple[str, ...], ...] | None = None,
    cap_grid: tuple[tuple[str, ...], ...] | None = None,
) -> str:
    """
    The cap an operator presses to produce ``token``.

    Falls back to the token itself, so a legend built from a remapped keypad
    that has lost a function still says something rather than nothing.
    """
    if grid is None or cap_grid is None:
        configured_grid, configured_caps = key_tables()
        grid = grid or configured_grid
        cap_grid = cap_grid or configured_caps

    for row_index, row in enumerate(grid):
        for col_index, cell in enumerate(row):
            if cell == token:
                try:
                    return cap_grid[row_index][col_index]
                except IndexError:
                    return token
    return token


def producible_characters(grid: tuple[tuple[str, ...], ...] | None = None) -> set[str]:
    """Every character this keypad can put into an answer."""
    return {
        token
        for row in (grid or layout())
        for token in row
        if token not in CONTROL_TOKENS
    }


def describe(grid: tuple[tuple[str, ...], ...] | None = None) -> list[str]:
    """
    The layout as a picture, for the menu and for bring-up.

    Each cell shows the cap and what that key emits, because the whole class of
    bug this is here to catch is the two not agreeing.
    """
    configured_grid, cap_grid = key_tables()
    grid = grid or configured_grid

    lines = ["      " + "".join(f"{label:^11}" for label in COL_LABELS)]
    for row_index, (label, row) in enumerate(zip(ROW_LABELS, grid)):
        cells = []
        for col_index, token in enumerate(row):
            try:
                cap = cap_grid[row_index][col_index]
            except IndexError:
                cap = "?"
            cells.append(f"{cap}={token}" if cap != token else token)
        lines.append(f"  {label}   " + "".join(f"{cell:^11}" for cell in cells))
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
