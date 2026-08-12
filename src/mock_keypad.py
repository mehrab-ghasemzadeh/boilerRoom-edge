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

from keypad_layout import ENTER, LineEditor, describe, layout, producible_characters


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

    async def start(self) -> None:
        if self._emulate:
            self.name = "keyboard (as the device's keypad)"

    async def close(self) -> None:
        pass

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
        if not self._emulate:
            return ["Input: keyboard (mock hardware)"]
        return ["Input: keyboard, restricted to the device's keypad:"] + describe(
            self._grid
        )
