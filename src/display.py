"""
ST7920 128x64 graphical LCD, driven over SPI.

Wiring (BCM numbering)::

    ST7920      Pi
    SID    ->   GPIO10  MOSI
    CLK    ->   GPIO11  SCLK
    CS     ->   GPIO7   CE1
    VCC    ->   5V
    GND    ->   GND

The panel shares the SPI bus with the gas ADC, which sits on CE0. Chip selects
are separate and the kernel serialises transfers, so the two do not need to
know about each other — but they do run at different clocks and in different
SPI modes, which is why each opens its own ``SpiDev`` handle rather than
sharing one.

Two things about this controller are easy to get wrong and expensive to debug:

* **CS is active HIGH.** Every other SPI peripheral on the header asserts chip
  select low; the ST7920 asserts it high. ``spidev`` can be told this, and is,
  below. A panel that stays blank with a perfectly good signal on SID and CLK
  is almost always this.
* **PSB must be tied LOW** for serial mode. It is a jumper or a solder pad on
  the module — not a pin on the five-wire connector — and with it left high the
  controller is listening for a parallel bus that is not there.

Refreshes send only the rows that changed. A full frame is 64 rows of 16 bytes,
which becomes about 2.5 KB once each byte is split into the two padded nibbles
the serial protocol wants — 25 ms of bus time at 800 kHz, on a core that also
has boilers to run. Moving the selection down a menu changes two rows, so it
costs about a millisecond.

Every SPI call blocks, so each one goes through ``asyncio.to_thread`` like the
rest of the hardware here.
"""

from __future__ import annotations

import asyncio
import os
import time

import spidev

from config import DISPLAY_SPI_BUS, DISPLAY_SPI_DEVICE, DISPLAY_SPI_SPEED
from display_canvas import BYTES_PER_ROW, HEIGHT, WIDTH, Canvas

# Sync bytes that open a serial transfer: bit pattern 11111, then RW and RS.
_SYNC_COMMAND = 0xF8
_SYNC_DATA = 0xFA

# Basic instruction set
_CMD_FUNCTION_BASIC = 0x30
_CMD_DISPLAY_ON = 0x0C
_CMD_CLEAR = 0x01
_CMD_ENTRY_MODE = 0x06

# Extended instruction set: graphics off, then graphics on.
_CMD_FUNCTION_EXTENDED = 0x34
_CMD_GRAPHICS_ON = 0x36

# The controller needs 1.6 ms to clear its text RAM, and about 72 us for the
# rest. Only the init sequence waits — the graphics writes below are paced by
# the bus itself.
_CLEAR_DELAY = 0.002
_COMMAND_DELAY = 0.0001


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("on", "1", "true", "yes")


class DisplayError(RuntimeError):
    """Raised when the panel cannot be brought up."""


class ST7920Display:
    """The 128x64 panel on the SPI header."""

    name = "ST7920 128x64 LCD"
    width = WIDTH
    height = HEIGHT
    available = True

    def __init__(
        self,
        bus: int | None = None,
        device: int | None = None,
        speed_hz: int | None = None,
    ):
        self.bus = _env_int("BOILERROOM_DISPLAY_SPI_BUS", DISPLAY_SPI_BUS) if bus is None else bus
        self.device = (
            _env_int("BOILERROOM_DISPLAY_SPI_DEVICE", DISPLAY_SPI_DEVICE)
            if device is None
            else device
        )
        self.speed_hz = (
            _env_int("BOILERROOM_DISPLAY_SPI_SPEED", DISPLAY_SPI_SPEED)
            if speed_hz is None
            else speed_hz
        )
        self.cs_high = _env_flag("BOILERROOM_DISPLAY_CS_HIGH", True)

        self._spi: spidev.SpiDev | None = None
        # The last frame actually on the glass, so a refresh can send only what
        # moved. None forces the next flush to send everything.
        self._previous: bytearray | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        try:
            await asyncio.to_thread(self._open)
        except Exception as exc:
            raise DisplayError(
                f"could not open SPI {self.bus}.{self.device} for the display: {exc}"
            ) from exc

    def _open(self) -> None:
        spi = spidev.SpiDev()
        spi.open(self.bus, self.device)
        spi.max_speed_hz = self.speed_hz
        # Idle-low clock, sampled on the rising edge.
        spi.mode = 0b00

        if self.cs_high:
            # The ST7920 asserts chip select high, unlike everything else on
            # this header. Some kernels refuse the flag after open(); the panel
            # can still be driven with CS strapped high in hardware, so this is
            # not worth failing over.
            try:
                spi.cshigh = True
            except Exception:
                pass

        self._spi = spi
        self._previous = None
        self._initialise()

    def _initialise(self) -> None:
        time.sleep(0.05)  # the controller's own power-on settling time
        self._command(_CMD_FUNCTION_BASIC)
        self._command(_CMD_DISPLAY_ON)
        self._command(_CMD_CLEAR)
        time.sleep(_CLEAR_DELAY)
        self._command(_CMD_ENTRY_MODE)
        self._command(_CMD_FUNCTION_EXTENDED)
        self._command(_CMD_GRAPHICS_ON)
        self._blank()

    async def close(self) -> None:
        if self._spi is None:
            return
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        """
        Release the bus, leaving the last frame on the glass.

        An unpowered panel holds whatever it was last sent, and the menu draws
        a screen saying the agent has stopped before it gets here — which is
        worth more to whoever walks up to it than a blank display.
        """
        spi = self._spi
        self._spi = None
        if spi is None:
            return
        try:
            spi.close()
        except Exception:
            pass

    # -- serial protocol -----------------------------------------------------
    #
    # Each byte goes out as two bytes: the high nibble padded with zeros, then
    # the low nibble padded the same way. A burst of data needs only one sync
    # byte at the front, which is what makes a row of graphics RAM affordable.

    def _command(self, value: int) -> None:
        spi = self._spi
        if spi is None:
            return
        spi.writebytes([_SYNC_COMMAND, value & 0xF0, (value << 4) & 0xF0])
        time.sleep(_COMMAND_DELAY)

    def _data(self, payload: bytes) -> None:
        spi = self._spi
        if spi is None:
            return
        message = [_SYNC_DATA]
        for byte in payload:
            message.append(byte & 0xF0)
            message.append((byte << 4) & 0xF0)
        spi.writebytes(message)

    def _set_address(self, row: int) -> None:
        """
        Point the controller at one 128-pixel row of graphics RAM.

        The address is a vertical 0-31 and a horizontal word 0-15. The lower
        half of the panel is not addressed as rows 32-63: it is the same 32
        vertical addresses, at horizontal words 8-15.
        """
        self._command(0x80 | (row & 0x1F))
        self._command(0x80 | (0 if row < 32 else 8))

    def _blank(self) -> None:
        empty = bytes(BYTES_PER_ROW)
        for row in range(HEIGHT):
            self._set_address(row)
            self._data(empty)
        self._previous = bytearray(BYTES_PER_ROW * HEIGHT)

    # -- drawing -------------------------------------------------------------

    async def show(self, canvas: Canvas) -> int:
        """Push a frame. Returns how many rows had to be sent."""
        if self._spi is None:
            return 0
        return await asyncio.to_thread(self._flush, canvas.snapshot())

    def _flush(self, frame: bytes) -> int:
        previous = self._previous
        sent = 0

        for row in range(HEIGHT):
            start = row * BYTES_PER_ROW
            end = start + BYTES_PER_ROW
            chunk = frame[start:end]
            if previous is not None and previous[start:end] == chunk:
                continue
            self._set_address(row)
            self._data(chunk)
            sent += 1

        self._previous = bytearray(frame)
        return sent

    async def clear(self) -> None:
        if self._spi is None:
            return
        await asyncio.to_thread(self._blank)

    def describe(self) -> list[str]:
        return [
            f"Display: {self.name} on SPI {self.bus}.{self.device} "
            f"at {self.speed_hz / 1000:.0f} kHz"
            + (", CS active high" if self.cs_high else "")
        ]
