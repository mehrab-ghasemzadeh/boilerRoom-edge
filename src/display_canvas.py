"""
The 128x64 one-bit framebuffer everything is drawn into.

Pure Python, no hardware: a canvas can be built and inspected anywhere, which
is what lets the mock display render the very same frame as ASCII on a laptop.

Byte order matches the ST7920's graphics RAM exactly — sixteen bytes per row,
the leftmost pixel in bit 7 of byte 0 — so pushing a frame is a straight copy
of a row slice rather than a per-pixel translation. That matters on a single
ARMv6 core: the alternative is 8192 shifts per refresh.

Filled and inverted areas are done a byte at a time with edge masks rather than
pixel by pixel. A title bar, a legend strip and a selected row add up to about
three thousand pixels a frame, and the menu redraws on every keypress.
"""

from __future__ import annotations

from display_font import CELL_HEIGHT, CELL_WIDTH, GLYPH_HEIGHT, GLYPH_WIDTH, glyph

WIDTH = 128
HEIGHT = 64
BYTES_PER_ROW = WIDTH // 8

# What fits across the panel, for callers deciding how much text to build.
COLUMNS = WIDTH // CELL_WIDTH  # 21
ROWS = HEIGHT // CELL_HEIGHT  # 8

_SET = "set"
_CLEAR = "clear"
_INVERT = "invert"


class Canvas:
    """A mutable 128x64 monochrome frame."""

    width = WIDTH
    height = HEIGHT

    def __init__(self) -> None:
        self.buffer = bytearray(BYTES_PER_ROW * HEIGHT)

    # -- whole-frame ---------------------------------------------------------

    def clear(self, on: bool = False) -> None:
        fill = 0xFF if on else 0x00
        for index in range(len(self.buffer)):
            self.buffer[index] = fill

    def snapshot(self) -> bytes:
        return bytes(self.buffer)

    # -- primitives ----------------------------------------------------------

    def pixel(self, x: int, y: int, on: bool = True) -> None:
        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
            return
        index = y * BYTES_PER_ROW + (x >> 3)
        mask = 0x80 >> (x & 7)
        if on:
            self.buffer[index] |= mask
        else:
            self.buffer[index] &= 0xFF ^ mask

    def _span(self, x: int, y: int, width: int, height: int, mode: str) -> None:
        """Apply one mode across a rectangle, a byte at a time."""
        if width <= 0 or height <= 0:
            return

        # Clip rather than raise: callers work in a layout built from constants,
        # and a screen that draws one pixel off the edge should still draw.
        if x < 0:
            width += x
            x = 0
        if y < 0:
            height += y
            y = 0
        width = min(width, WIDTH - x)
        height = min(height, HEIGHT - y)
        if width <= 0 or height <= 0:
            return

        first = x >> 3
        last = (x + width - 1) >> 3

        masks = []
        for index in range(first, last + 1):
            mask = 0xFF
            if index == first:
                mask &= 0xFF >> (x & 7)
            if index == last:
                mask &= (0xFF << (7 - ((x + width - 1) & 7))) & 0xFF
            masks.append((index, mask))

        buffer = self.buffer
        for row in range(y, y + height):
            base = row * BYTES_PER_ROW
            for index, mask in masks:
                position = base + index
                if mode is _SET:
                    buffer[position] |= mask
                elif mode is _CLEAR:
                    buffer[position] &= 0xFF ^ mask
                else:
                    buffer[position] ^= mask

    def fill_rect(self, x: int, y: int, width: int, height: int, on: bool = True) -> None:
        self._span(x, y, width, height, _SET if on else _CLEAR)

    def invert_rect(self, x: int, y: int, width: int, height: int) -> None:
        self._span(x, y, width, height, _INVERT)

    def hline(self, x: int, y: int, width: int, on: bool = True) -> None:
        self._span(x, y, width, 1, _SET if on else _CLEAR)

    def vline(self, x: int, y: int, height: int, on: bool = True) -> None:
        self._span(x, y, 1, height, _SET if on else _CLEAR)

    def rect(self, x: int, y: int, width: int, height: int, on: bool = True) -> None:
        """Outline only."""
        if width <= 0 or height <= 0:
            return
        self.hline(x, y, width, on)
        self.hline(x, y + height - 1, width, on)
        self.vline(x, y, height, on)
        self.vline(x + width - 1, y, height, on)

    # -- text ----------------------------------------------------------------

    def text(self, x: int, y: int, text: str, on: bool = True) -> int:
        """
        Draw a string with its top-left corner at ``(x, y)``. Returns the x the
        next character would start at.

        ``on=False`` clears pixels instead of setting them, which is how text
        goes into a filled bar — the title and the legend are drawn that way.
        """
        cursor = x
        for character in text:
            if cursor >= WIDTH:
                break
            columns = glyph(character)
            for column_index in range(GLYPH_WIDTH):
                column = columns[column_index]
                if not column:
                    continue
                pixel_x = cursor + column_index
                if not 0 <= pixel_x < WIDTH:
                    continue
                for row_index in range(GLYPH_HEIGHT):
                    if column & (1 << row_index):
                        self.pixel(pixel_x, y + row_index, on)
            cursor += CELL_WIDTH
        return cursor

    def text_centered(self, y: int, text: str, on: bool = True) -> None:
        x = max(0, (WIDTH - text_width(text)) // 2)
        self.text(x, y, text, on)

    def text_right(self, right: int, y: int, text: str, on: bool = True) -> None:
        self.text(max(0, right - text_width(text)), y, text, on)

    # -- small shapes the chrome needs ---------------------------------------

    def triangle_up(self, x: int, y: int, on: bool = True) -> None:
        """A 5x3 arrowhead, used in the legend and the scroll indicators."""
        self.pixel(x + 2, y, on)
        self._span(x + 1, y + 1, 3, 1, _SET if on else _CLEAR)
        self._span(x, y + 2, 5, 1, _SET if on else _CLEAR)

    def triangle_down(self, x: int, y: int, on: bool = True) -> None:
        self._span(x, y, 5, 1, _SET if on else _CLEAR)
        self._span(x + 1, y + 1, 3, 1, _SET if on else _CLEAR)
        self.pixel(x + 2, y + 2, on)


def text_width(text: str) -> int:
    """Pixels one string occupies, including the gap after the last glyph."""
    return len(text) * CELL_WIDTH


def fits(text: str, pixels: int) -> bool:
    return text_width(text) <= pixels


def truncate(text: str, columns: int) -> str:
    """
    Shorten to ``columns`` characters, marking that something was cut.

    An ellipsis rather than a hard cut: on a 21-column panel plenty of lines
    are one or two characters too long, and the difference between "Max water
    temperatur" and "Max water temperat~" is knowing there is more.
    """
    if columns <= 0:
        return ""
    if len(text) <= columns:
        return text
    if columns == 1:
        return "~"
    return text[: columns - 1] + "~"


def wrap(text: str, columns: int) -> list[str]:
    """
    Break one line to fit the panel, on spaces where it can.

    Continuation lines keep the original indentation plus two spaces, so the
    nested output the menu already produces still reads as nested once it has
    been folded. An empty line survives as an empty line: the menu uses blank
    lines as separators and losing them runs everything together.
    """
    if columns <= 0:
        return [""]

    text = text.rstrip()
    if not text:
        return [""]
    if len(text) <= columns:
        return [text]

    indent = len(text) - len(text.lstrip())
    continuation = " " * min(indent + 2, max(0, columns - 4))

    lines: list[str] = []
    remaining = text
    prefix = ""

    while remaining:
        room = columns - len(prefix)
        if room <= 0:  # pathological indentation; give up on it
            prefix = ""
            room = columns

        if len(remaining) <= room:
            lines.append(prefix + remaining)
            break

        cut = remaining.rfind(" ", 0, room + 1)
        if cut <= 0:
            cut = room  # one long unbroken token: split it rather than overflow

        lines.append(prefix + remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
        prefix = continuation

    return lines or [""]


def wrap_all(lines: list[str], columns: int) -> list[str]:
    """Wrap a block of lines, collapsing runs of blanks to a single one."""
    wrapped: list[str] = []
    for line in lines:
        for part in wrap(line.replace("\t", "    "), columns):
            if not part and (not wrapped or not wrapped[-1]):
                continue  # no double blank lines; the panel has six rows
            wrapped.append(part)
    while wrapped and not wrapped[-1]:
        wrapped.pop()
    return wrapped
