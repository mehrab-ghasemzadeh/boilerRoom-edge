"""
The graphical display, standing in for it on a machine that has no panel.

Selected by USE_MOCK_HARDWARE alongside the mock relays and sensor readers.
By default it is *not available*, and the control menu then behaves exactly as
it always has: numbered options typed at a terminal. Nothing about bench work
changes until you ask for it.

``BOILERROOM_DISPLAY=mock`` turns it on. The same frames the ST7920 would be
sent are then drawn in the terminal as text, and the menu switches to the
scroll-and-select interface the device uses — which is the point. A menu that
only works on a panel nobody can test is a menu that is wrong on the day it
reaches the boiler room.

Two pixel rows share one character cell using half blocks, so a 128x64 frame
prints as 32 lines rather than 64. Where the terminal cannot encode those, an
ASCII approximation is used instead.
"""

from __future__ import annotations

import asyncio
import os
import sys

from display_canvas import BYTES_PER_ROW, HEIGHT, WIDTH, Canvas

_BLOCKS = {
    (False, False): " ",
    (True, False): "▀",  # upper half
    (False, True): "▄",  # lower half
    (True, True): "█",  # full
}

_ASCII_BLOCKS = {
    (False, False): " ",
    (True, False): '"',
    (False, True): ".",
    (True, True): "#",
}


def preview_enabled() -> bool:
    return (os.environ.get("BOILERROOM_DISPLAY") or "").strip().lower() in (
        "mock",
        "preview",
        "on",
        "1",
        "true",
        "yes",
    )


def _blocks_encodable() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(_BLOCKS.values()).encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


class MockDisplay:
    """Draws the frame in the terminal instead of on the glass."""

    name = "terminal preview of the 128x64 panel"
    width = WIDTH
    height = HEIGHT

    def __init__(self, writer=None):
        self.available = preview_enabled()
        self._writer = writer
        self._blocks = _BLOCKS if _blocks_encodable() else _ASCII_BLOCKS
        self._previous: bytes | None = None

    def set_writer(self, writer) -> None:
        """
        Route frames through the caller's output instead of ``print``.

        The menu already serialises its terminal writes behind a lock; a frame
        drawn straight to stdout would interleave with them.
        """
        self._writer = writer

    async def start(self) -> None:
        self._previous = None

    async def close(self) -> None:
        pass

    async def clear(self) -> None:
        self._previous = None

    async def show(self, canvas: Canvas) -> int:
        frame = canvas.snapshot()
        if frame == self._previous:
            return 0
        self._previous = frame

        await self._write("\n".join(self._render(frame)))
        return HEIGHT

    def _render(self, frame: bytes) -> list[str]:
        border = "+" + "-" * WIDTH + "+"
        lines = [border]

        for top in range(0, HEIGHT, 2):
            cells = []
            for x in range(WIDTH):
                index = x >> 3
                mask = 0x80 >> (x & 7)
                upper = bool(frame[top * BYTES_PER_ROW + index] & mask)
                lower = bool(frame[(top + 1) * BYTES_PER_ROW + index] & mask)
                cells.append(self._blocks[(upper, lower)])
            lines.append("|" + "".join(cells) + "|")

        lines.append(border)
        return lines

    async def _write(self, text: str) -> None:
        if self._writer is not None:
            await self._writer(text)
            return
        await asyncio.to_thread(print, text)

    def describe(self) -> list[str]:
        if not self.available:
            return []
        return [f"Display: {self.name}"]
