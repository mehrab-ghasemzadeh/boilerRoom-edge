"""
The keypad, standing in for it with the keyboard of whatever this runs on.

Selected by USE_MOCK_HARDWARE alongside the mock relay and sensor readers, so
bench work keeps the terminal it has always had: type an answer, press Enter.
Blocking input runs in a worker thread, like every other blocking call here.

Emulation
---------
BOILERROOM_KEYPAD_EMULATE=on puts the typed line through the same key table and
line editor the GPIO keypad uses, one character at a time, and refuses anything
the keypad has no key for. Nothing about the flow changes — the point is to
find prompts that cannot be answered from the device *before* the code is on a
device with no keyboard attached.
"""

from __future__ import annotations

import asyncio
import os

from keypad_layout import (
    CANCEL,
    ENTER,
    LineEditor,
    describe,
    key_tables,
    layout,
    producible_characters,
)
from mock_display import preview_enabled

# Typed on a keyboard, meaning the key whose cap says this. Lets the display's
# screens be driven from the bench exactly as they are from the pad: "88#"
# moves down twice and selects.
_TYPED_ALIASES = {
    "#": ENTER,
    "*": CANCEL,
    # No cap says "up" or "down", but a keyboard has better ideas about it than
    # a membrane pad does.
    "w": "2",
    "s": "8",
}


def _emulation_enabled() -> bool:
    return (os.environ.get("BOILERROOM_KEYPAD_EMULATE") or "").strip().lower() in (
        "on",
        "1",
        "true",
        "yes",
    )


class MockKeypad:
    """Reads answers from the keyboard instead of from the GPIO matrix."""

    name = "keyboard"
    # There has to be a terminal to type into: with no TTY, input() raises
    # EOFError straight away and the menu would read that as "quit".
    needs_tty = True

    def __init__(self, *, emulate: bool | None = None):
        self._emulate = _emulation_enabled() if emulate is None else emulate
        self._grid = layout() if self._emulate else None
        # Keys typed as one line and handed out one at a time; see read_key.
        self._pending: list[str] = []

    async def start(self) -> None:
        if self._emulate:
            self.name = "keyboard (as the device's keypad)"

    async def close(self) -> None:
        pass

    async def read_key(self) -> str:
        """
        One keypress, taken from a typed line.

        The display's screens want keys one at a time, and a keyboard hands
        over a whole line at once. So a line is broken into the keys it stands
        for and served from a buffer: typing ``8`` then ``#`` is two presses,
        and typing ``88#`` is three. An empty line is the accept key, which is
        what pressing Enter on its own already meant everywhere else.

        Keypad tokens come out of the same table the real pad uses, so what
        works here works there.
        """
        while not self._pending:
            raw = await asyncio.to_thread(input, "keys> ")
            self._pending = self._tokens(raw.strip())

        return self._pending.pop(0)

    def _tokens(self, raw: str) -> list[str]:
        if not raw:
            return [ENTER]

        configured_grid, cap_grid = key_tables()
        by_cap = {
            cap: token
            for cap_row, token_row in zip(cap_grid, self._grid or configured_grid)
            for cap, token in zip(cap_row, token_row)
        }

        tokens: list[str] = []
        for character in raw:
            lowered = character.lower()
            if lowered in _TYPED_ALIASES:
                tokens.append(_TYPED_ALIASES[lowered])
            elif character in by_cap:
                tokens.append(by_cap[character])
            elif lowered in by_cap:
                tokens.append(by_cap[lowered])
            else:
                # Not a key this pad has. Passed through rather than dropped:
                # the screen it reaches ignores what it does not recognise, and
                # silently swallowing keys makes a stuck menu look like a bug.
                tokens.append(character)
        return tokens

    async def read_line(self, prompt: str) -> str:
        raw = await asyncio.to_thread(input, prompt)
        if not self._emulate:
            return raw.strip()
        return await self._through_the_keypad(raw.strip())

    async def _through_the_keypad(self, raw: str) -> str:
        usable = producible_characters(self._grid)

        unreachable = sorted({c for c in raw if c not in usable})
        if unreachable:
            # Deliberately answers nothing rather than accepting it anyway: an
            # operator at the boiler could not have typed this, and a prompt
            # that only works on the bench is the thing worth finding.
            await asyncio.to_thread(
                print,
                f"[keypad] No key for {', '.join(repr(c) for c in unreachable)} — "
                "this answer is not reachable from the device's keypad. "
                "Treating it as cancelled.",
            )
            return ""

        editor = LineEditor()
        for character in raw:
            editor.feed(character)
        editor.feed(ENTER)
        return editor.text

    def describe(self) -> list[str]:
        lines = (
            ["Input: keyboard (mock hardware)"]
            if not self._emulate
            else ["Input: keyboard, restricted to the device's keypad:"]
            + describe(self._grid)
        )
        if preview_enabled():
            lines.append(
                "At 'keys>' type the caps you would press — 8 moves down, "
                "88# moves down twice and selects, Enter on its own is #."
            )
        return lines
