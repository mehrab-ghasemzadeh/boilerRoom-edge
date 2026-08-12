"""
4x4 matrix keypad on the GPIO header.

This is the control menu's input device on the real hardware, in place of the
keyboard: an operator standing at the boiler has no terminal, and every menu
already asks for a number, so a keypad is enough to reach all of it. See
``keypad_layout`` for the pin map, the key table and the line editor.

Scanning runs on its own thread, not on the shared ``asyncio.to_thread``
executor. That pool has a handful of workers on a single-core Pi and the
control menu already parks one of them in ``input()`` forever; a second
permanent occupant would start starving the file writes and hardware reads that
pass through it. Keys reach the event loop by ``call_soon_threadsafe``.

Cost: idle, a poll drives nothing and reads four pins, 25 times a second — a
few hundred microseconds of CPU per second on a Pi Zero W. The full row-by-row
scan only runs while a key is actually down. Interrupts would be cheaper still,
but RPi.GPIO's edge detection has to be torn down and rebuilt around each scan
of the same pins, which is both slower and more fragile than the polling it
would replace.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import RPi.GPIO as GPIO

from keypad_layout import (
    COL_LABELS,
    CONTROL_TOKENS,
    ROW_LABELS,
    KeypadLayoutError,
    LineEditor,
    col_pins,
    describe,
    layout,
    row_pins,
)

# How often to ask "is anything down?". Fast enough that a normal press is never
# missed, slow enough to cost nothing.
IDLE_POLL_SECONDS = 0.04

# A membrane key bounces for a few milliseconds. The press is confirmed by a
# second scan this long after the first, and the same gap is left after release
# before another key is accepted.
DEBOUNCE_SECONDS = 0.02

# Time for a driven row to settle before its columns are read.
SETTLE_SECONDS = 0.001

# Pushed onto the queue by close(), so a pending read_line returns instead of
# waiting for a key that will never come.
_CLOSED = object()


class KeypadError(RuntimeError):
    """Raised when the keypad cannot be brought up."""


class Keypad:
    """The control menu's input device, backed by the GPIO matrix."""

    name = "GPIO matrix keypad"
    # The keypad is the terminal. Without this the menu would be disabled under
    # systemd, where there is no TTY — which is exactly where the keypad is the
    # only way in.
    needs_tty = False

    def __init__(
        self,
        rows: tuple[int, ...] | None = None,
        cols: tuple[int, ...] | None = None,
        grid: tuple[tuple[str, ...], ...] | None = None,
    ):
        try:
            self._rows = rows or row_pins()
            self._cols = cols or col_pins()
            self._grid = grid or layout()
        except KeypadLayoutError as exc:
            raise KeypadError(str(exc)) from exc

        overlap = set(self._rows) & set(self._cols)
        if overlap:
            raise KeypadError(
                f"GPIO {sorted(overlap)} is listed as both a row and a column"
            )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def conflicting_pins(self) -> dict[int, str]:
        """
        Pins this keypad wants that something else already drives.

        Scanning drives the row pins LOW and HIGH. If a row shares a pin with a
        relay, pressing a key switches a boiler — so this is checked before the
        pins are claimed, not diagnosed afterwards from a boiler that turns
        itself on. The wiring comes from the server record, so it can change
        under a device that was correctly configured when it was installed.

        Only meaningful once a mapping exists; an unpaired device has no relays
        to clash with yet.
        """
        from config import ONE_WIRE_GPIO, RELAYS

        mine = set(self._rows) | set(self._cols)
        clashes: dict[int, str] = {}

        for relay_id, cfg in sorted(RELAYS.items()):
            gpio = cfg.get("gpio")
            if gpio in mine:
                clashes[gpio] = f"relay {relay_id} ({cfg.get('name') or cfg.get('role')})"

        if ONE_WIRE_GPIO in mine:
            clashes[ONE_WIRE_GPIO] = "the 1-Wire temperature bus"

        return clashes

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._stop.clear()

        clashes = self.conflicting_pins()
        if clashes:
            raise KeypadError(
                "these pins already drive something else — "
                + "; ".join(f"GPIO {pin} is {what}" for pin, what in sorted(clashes.items()))
                + ". Refusing to scan them: a keypress on a relay pin switches "
                "that relay. Move the keypad with BOILERROOM_KEYPAD_ROWS/_COLS, "
                "or correct the wiring in the server record"
            )

        try:
            await asyncio.to_thread(self._setup_gpio)
        except Exception as exc:  # a wrong pin number, or no permission
            raise KeypadError(f"could not claim the keypad pins: {exc}") from exc

        self._thread = threading.Thread(
            target=self._scan_forever, name="keypad", daemon=True
        )
        self._thread.start()

    def _setup_gpio(self) -> None:
        GPIO.setmode(GPIO.BCM)
        # The relay controller sets up its own pins; without this, whichever of
        # the two runs second warns about every channel the first claimed.
        GPIO.setwarnings(False)
        for pin in self._rows:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
        for pin in self._cols:
            # A key connects a column to a row, and rows idle LOW — so a column
            # has to be held HIGH by something for a press to be a change.
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    async def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, 1.0)

        if self._queue is not None:
            self._queue.put_nowait(_CLOSED)

        await asyncio.to_thread(self._cleanup_gpio)

    def _cleanup_gpio(self) -> None:
        # Only this module's pins: a bare GPIO.cleanup() would also release the
        # relay pins, dropping every relay mid-run.
        try:
            GPIO.cleanup(list(self._rows) + list(self._cols))
        except Exception:
            pass

    # -- scanning ------------------------------------------------------------

    def _any_pressed(self) -> bool:
        """Cheap check with every row held LOW: is any column pulled down?"""
        return any(GPIO.input(pin) == GPIO.LOW for pin in self._cols)

    def _scan_once(self) -> str | None:
        """
        Which single key is down, row by row.

        Two keys at once is refused rather than guessed at: an undiode'd matrix
        cannot tell a genuine third press from the ghost the other two produce.
        """
        found: list[str] = []

        for row_index, row_pin in enumerate(self._rows):
            for other in self._rows:
                GPIO.output(other, GPIO.HIGH if other != row_pin else GPIO.LOW)
            time.sleep(SETTLE_SECONDS)

            for col_index, col_pin in enumerate(self._cols):
                if GPIO.input(col_pin) == GPIO.LOW:
                    found.append(self._grid[row_index][col_index])

        for pin in self._rows:  # back to idle, so _any_pressed works again
            GPIO.output(pin, GPIO.LOW)

        return found[0] if len(found) == 1 else None

    def _scan_forever(self) -> None:
        while not self._stop.is_set():
            if not self._any_pressed():
                self._stop.wait(IDLE_POLL_SECONDS)
                continue

            key = self._scan_once()
            if key is None:
                self._stop.wait(IDLE_POLL_SECONDS)
                continue

            # Confirm through the bounce before acting on it.
            self._stop.wait(DEBOUNCE_SECONDS)
            if self._scan_once() != key:
                continue

            self._emit(key)

            # One press, one key: hold off until the operator lets go, or a
            # finger resting on a button would fill the buffer.
            while not self._stop.is_set() and self._any_pressed():
                self._stop.wait(IDLE_POLL_SECONDS)
            self._stop.wait(DEBOUNCE_SECONDS)

    def _emit(self, key: str) -> None:
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, key)
        except RuntimeError:
            pass  # the loop closed while we were scanning; shutting down

    # -- reading -------------------------------------------------------------

    async def read_line(self, prompt: str) -> str:
        """
        Collect keypresses into one answer, the way ``input()`` collects typing.

        Raises EOFError once the keypad is closed, which is what the menu loop
        already treats as "no input device left".
        """
        if self._queue is None:
            raise EOFError("keypad is not running")

        editor = LineEditor()
        await _write(prompt)

        # Only the last line of the prompt is redrawn as keys arrive: the main
        # menu is a ten-line block, and reprinting all of it per keypress would
        # bury the answer being typed.
        tail = prompt.rsplit("\n", 1)[-1]

        while True:
            key = await self._queue.get()
            if key is _CLOSED:
                raise EOFError("keypad closed")
            if editor.feed(key):
                break
            # Rewritten in place so the buffer is visible on a console or over
            # SSH. With no display attached this is simply ignored.
            await _write("\r" + tail + editor.text + "\x1b[K")

        await _write("\n")
        return editor.text

    def describe(self) -> list[str]:
        lines = [f"Keypad rows (A-D): GPIO {', '.join(str(p) for p in self._rows)}"]
        lines.append(f"Keypad cols (1-4): GPIO {', '.join(str(p) for p in self._cols)}")
        lines.extend(describe(self._grid))
        return lines


async def _write(text: str) -> None:
    await asyncio.to_thread(_write_sync, text)


def _write_sync(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass  # no terminal attached: the keypad still works, we just can't echo


# ---------------------------------------------------------------------------
# Bring-up
# ---------------------------------------------------------------------------
#
# What is printed on a key cap says nothing about where it sits in the matrix,
# and a calculator pad is not laid out like the membrane pad this defaults to.
# Run this, press every key, and set BOILERROOM_KEYPAD_LAYOUT to what you see.


async def _test_mode() -> None:
    keypad = Keypad()
    print("\n".join(keypad.describe()))
    print(
        "\nPress keys — each one prints the token it currently produces.\n"
        "Ctrl-C to stop.\n"
    )
    await keypad.start()
    try:
        while True:
            key = await keypad._queue.get()  # noqa: SLF001 — this is the owner
            if key is _CLOSED:
                return
            kind = "control" if key in CONTROL_TOKENS else "character"
            position = _position(keypad, key)
            print(f"  {key!r:<10} {kind:<10} {position}")
    finally:
        await keypad.close()


def _position(keypad: Keypad, token: str) -> str:
    for row_index, row in enumerate(keypad._grid):  # noqa: SLF001
        for col_index, cell in enumerate(row):
            if cell == token:
                return f"row {ROW_LABELS[row_index]}, column {COL_LABELS[col_index]}"
    return "?"


if __name__ == "__main__":
    if "--test" not in sys.argv:
        print("Usage: python src/keypad.py --test")
        raise SystemExit(2)
    try:
        asyncio.run(_test_mode())
    except KeyboardInterrupt:
        print()
