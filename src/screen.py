"""
The screens drawn on the graphical display, and the keys that drive them.

The control menu was written for a terminal: print a numbered list, read a
whole typed line back. That works on a 24-line console and not at all on a
panel six rows tall, so this module gives the same menu a second face — one
highlighted row at a time, moved with the keypad and confirmed with a key,
with the list scrolling underneath the selection.

Four keys do everything, and the strip along the bottom of every screen says
which four, because an operator standing at a boiler has no manual and the caps
say ``2`` and ``8``, not "up" and "down":

  ``2``  move up          ``#``  select / accept
  ``8``  move down        ``*``  back

The layout is fixed at 128x64 with a 6x8 cell: a title bar, six body rows, and
the legend strip. Anything longer than six rows scrolls, and a bar down the
right-hand edge shows how far through it you are — on a screen this small the
difference between "the list ends here" and "there are nine more" is otherwise
invisible.

Nothing here knows what a boiler is. Screens are given lines and items; the
menu builds them.
"""

from __future__ import annotations

from display_canvas import Canvas, WIDTH, truncate, wrap_all
from keypad_layout import (
    CANCEL,
    DEL,
    ENTER,
    SCROLL_DOWN,
    SCROLL_UP,
    LineEditor,
    cap_for,
)

# -- geometry ---------------------------------------------------------------

TITLE_HEIGHT = 8
LEGEND_HEIGHT = 8
ROW_HEIGHT = 8

BODY_TOP = TITLE_HEIGHT
BODY_ROWS = 6
BODY_HEIGHT = BODY_ROWS * ROW_HEIGHT
LEGEND_TOP = BODY_TOP + BODY_HEIGHT  # 56

# The right-hand gutter the scroll bar lives in. Selection highlights stop
# short of it, so the bar stays readable on a highlighted row.
GUTTER = 4
BODY_WIDTH = WIDTH - GUTTER

TEXT_X = 2
# Glyphs are 7 px in an 8 px cell; the spare row goes above, so a highlighted
# row has a margin at the top and sits flush with the row below it.
TEXT_OFFSET = 1

BODY_COLUMNS = (BODY_WIDTH - TEXT_X) // 6  # 20
BAR_COLUMNS = (WIDTH - TEXT_X * 2) // 6  # 20


# The scroll keys, drawn as their caps with an arrowhead beside each rather
# than spelled out. "2/8 Move" is eight of the twenty-one columns a legend
# has; this is four, and an arrow says which way it goes better than the word
# does.
SCROLL_KEYS = ("\x01scroll", "")

_SCROLL_WIDTH = 6 + 6 + 4 + 6 + 6  # cap, arrow, gap, cap, arrow


def scroll_legend(*extra: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    """The standard strip: move, select, back — plus anything a screen adds."""
    return (
        SCROLL_KEYS,
        (cap_for(ENTER), "OK"),
        (cap_for(CANCEL), "Back"),
    ) + extra


def _entry_width(entry: tuple[str, str]) -> int:
    if entry == SCROLL_KEYS:
        return _SCROLL_WIDTH
    cap, label = entry
    return len(f"{cap} {label}" if label else cap) * 6


class Screen:
    """Draws screens on a display and reads the keypad that answers them."""

    def __init__(self, display, device, *, echo=None):
        self.display = display
        self.device = device
        self.canvas = Canvas()

        # The mock display prints its frames; routing them through the menu's
        # own output keeps them from interleaving with it.
        set_writer = getattr(display, "set_writer", None)
        if set_writer is not None and echo is not None:
            set_writer(echo)

    # -- plumbing ------------------------------------------------------------

    async def render(self) -> None:
        await self.display.show(self.canvas)

    async def _key(self) -> str:
        """One keypress. Raises EOFError once the input device has gone."""
        return await self.device.read_key()

    # -- chrome --------------------------------------------------------------

    def frame(
        self,
        title: str,
        *,
        right: str = "",
        legend: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Clear the canvas and draw the title bar and legend strip."""
        canvas = self.canvas
        canvas.clear()

        canvas.fill_rect(0, 0, WIDTH, TITLE_HEIGHT, True)
        room = BAR_COLUMNS
        if right:
            room = max(1, BAR_COLUMNS - len(right) - 1)
            canvas.text_right(WIDTH - TEXT_X, TEXT_OFFSET, right, on=False)
        canvas.text(TEXT_X, TEXT_OFFSET, truncate(title.upper(), room), on=False)

        self._legend(legend)

    def _legend(self, entries: tuple[tuple[str, str], ...]) -> None:
        """
        The strip along the bottom: which key does what, on this screen.

        Always drawn, and drawn last, so there is no screen an operator can
        reach without being told the way out of it.
        """
        canvas = self.canvas
        canvas.fill_rect(0, LEGEND_TOP, WIDTH, LEGEND_HEIGHT, True)
        if not entries:
            return

        room = WIDTH - TEXT_X * 2
        content = sum(_entry_width(entry) for entry in entries)

        # Widest spacing that still fits, then the caps on their own. A strip
        # that has been cut in half says less than nothing.
        for gap in (3, 2, 1):
            if content + gap * 6 * (len(entries) - 1) <= room:
                break
        else:
            gap = 1
            entries = tuple(
                entry if entry == SCROLL_KEYS else (entry[0], "") for entry in entries
            )
            content = sum(_entry_width(entry) for entry in entries)

        total = content + gap * 6 * (len(entries) - 1)
        x = max(TEXT_X, (WIDTH - total) // 2)
        y = LEGEND_TOP + TEXT_OFFSET

        for index, entry in enumerate(entries):
            if index:
                x += gap * 6
            if entry == SCROLL_KEYS:
                x = canvas.text(x, y, cap_for(SCROLL_UP), on=False)
                canvas.triangle_up(x, y + 2, on=False)
                x += 6 + 4
                x = canvas.text(x, y, cap_for(SCROLL_DOWN), on=False)
                canvas.triangle_down(x, y + 2, on=False)
                x += 6
                continue
            cap, label = entry
            x = canvas.text(x, y, f"{cap} {label}" if label else cap, on=False)

    def _scrollbar(self, top: int, visible: int, total: int) -> None:
        """A thumb on the right edge showing which slice of a list is shown."""
        if total <= visible:
            return

        canvas = self.canvas
        track_x = WIDTH - GUTTER + 1
        canvas.vline(track_x + 1, BODY_TOP, BODY_HEIGHT)

        height = max(4, BODY_HEIGHT * visible // total)
        span = max(1, total - visible)
        offset = (BODY_HEIGHT - height) * min(top, span) // span
        canvas.fill_rect(track_x, BODY_TOP + offset, 3, height, True)

    def _body_row(self, slot: int) -> int:
        return BODY_TOP + slot * ROW_HEIGHT

    # -- widgets -------------------------------------------------------------

    async def select(
        self,
        title: str,
        items: list[str],
        *,
        index: int = 0,
        legend: tuple[tuple[str, str], ...] | None = None,
    ) -> int | None:
        """
        Pick one row from a list. Returns its position, or None for back.

        The selection wraps at both ends: a list of eleven options is two
        presses from the bottom rather than nine, which on a keypad matters.
        """
        total = len(items)
        if total == 0:
            await self.message(title, ["Nothing to choose from."])
            return None

        index = max(0, min(index, total - 1))
        top = 0
        strip = scroll_legend() if legend is None else legend

        while True:
            # Keep the selection on screen, moving the window as little as it
            # takes — a list that jumps around under the highlight is unreadable.
            if index < top:
                top = index
            elif index >= top + BODY_ROWS:
                top = index - BODY_ROWS + 1
            top = max(0, min(top, max(0, total - BODY_ROWS)))

            self.frame(title, right=f"{index + 1}/{total}", legend=strip)

            for slot in range(BODY_ROWS):
                position = top + slot
                if position >= total:
                    break
                y = self._body_row(slot)
                selected = position == index
                if selected:
                    self.canvas.fill_rect(0, y, BODY_WIDTH, ROW_HEIGHT, True)
                self.canvas.text(
                    TEXT_X,
                    y + TEXT_OFFSET,
                    truncate(items[position], BODY_COLUMNS),
                    on=not selected,
                )

            self._scrollbar(top, BODY_ROWS, total)
            await self.render()

            key = await self._key()
            if key == SCROLL_UP:
                index = (index - 1) % total
            elif key == SCROLL_DOWN:
                index = (index + 1) % total
            elif key == ENTER:
                return index
            elif key == CANCEL:
                return None

    async def page(self, title: str, lines: list[str]) -> None:
        """
        Show a block of text, scrolling a row at a time.

        ``#`` pages forward while there is more below and closes the screen at
        the bottom, so holding one key reads the whole thing; ``*`` leaves at
        any point.
        """
        wrapped = wrap_all(lines, BODY_COLUMNS)
        if not wrapped:
            return

        total = len(wrapped)
        top = 0
        limit = max(0, total - BODY_ROWS)

        while True:
            at_end = top >= limit
            if total > BODY_ROWS:
                strip = (
                    SCROLL_KEYS,
                    (cap_for(ENTER), "Done" if at_end else "More"),
                    (cap_for(CANCEL), "Back"),
                )
            else:
                strip = ((cap_for(ENTER), "Done"), (cap_for(CANCEL), "Back"))

            right = f"{min(top + BODY_ROWS, total)}/{total}" if total > BODY_ROWS else ""
            self.frame(title, right=right, legend=strip)

            for slot in range(BODY_ROWS):
                position = top + slot
                if position >= total:
                    break
                self.canvas.text(
                    TEXT_X,
                    self._body_row(slot) + TEXT_OFFSET,
                    wrapped[position],
                )

            self._scrollbar(top, BODY_ROWS, total)
            await self.render()

            key = await self._key()
            if key == SCROLL_UP:
                top = max(0, top - 1)
            elif key == SCROLL_DOWN:
                top = min(limit, top + 1)
            elif key == CANCEL:
                return
            elif key == ENTER:
                if at_end:
                    return
                top = min(limit, top + BODY_ROWS)

    async def message(self, title: str, lines: list[str]) -> None:
        await self.page(title, lines)

    async def read_line(
        self,
        prompt: str,
        *,
        title: str = "Enter value",
        mask: bool = False,
    ) -> str:
        """
        Collect an answer, showing it as it is typed.

        The same ``LineEditor`` the keypad uses for a whole line, so ``*``
        abandons the answer and every prompt in the menu already reads an empty
        answer as "back".

        ``mask`` shows one dot per digit instead of the digits — for the device
        password, which is the only secret anybody types here. The count still
        shows, because on a keypad with no tactile feedback that is what tells
        an operator whether a press registered.
        """
        editor = LineEditor()
        strip = (
            (cap_for(ENTER), "OK"),
            (cap_for(DEL), "Del"),
            (cap_for(CANCEL), "Back"),
        )

        # The question is at the end of a prompt, so when one is too long to
        # fit it is the opening that gets dropped, not the ask.
        question = wrap_all([prompt.rstrip().rstrip(":")], BODY_COLUMNS)
        question = question[-(BODY_ROWS - 1) :]

        while True:
            self.frame(title, legend=strip)

            for slot, line in enumerate(question):
                self.canvas.text(TEXT_X, self._body_row(slot) + TEXT_OFFSET, line)

            entry_y = self._body_row(BODY_ROWS - 1)
            self.canvas.hline(0, entry_y - 1, WIDTH)

            text = "*" * len(editor.text) if mask else editor.text
            # Show the tail once an answer outgrows the row: what was just
            # typed is what needs checking.
            visible = text[-(BODY_COLUMNS - 2) :]
            end = self.canvas.text(TEXT_X + 2, entry_y + TEXT_OFFSET, visible)
            self.canvas.fill_rect(end, entry_y + 1, 4, ROW_HEIGHT - 2, True)

            await self.render()

            key = await self._key()
            if editor.feed(key):
                return editor.text

    async def splash(self, title: str, lines: list[str], *, legend=()) -> None:
        """Draw a screen and leave it there. Nothing is read."""
        self.frame(title, legend=legend)
        for slot, line in enumerate(lines[:BODY_ROWS]):
            self.canvas.text(
                TEXT_X,
                self._body_row(slot) + TEXT_OFFSET,
                truncate(line, BODY_COLUMNS),
            )
        await self.render()
