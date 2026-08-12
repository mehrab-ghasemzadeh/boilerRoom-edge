# Putting boilerRoom-edge on an Alpine Pi

A start-to-finish deployment: from an SD card with Alpine on it to a boiler room
that comes back on its own after a power cut.

Work through it in order. Each step ends with a **check** — if the check fails,
stop there rather than carrying the fault forward, because most of what goes
wrong later is one of these steps having quietly not worked.

---

## The one decision that shapes everything

Alpine's Raspberry Pi image runs **diskless** by default: the whole root
filesystem is unpacked into RAM at boot, and nothing written to it survives
unless you `lbu commit`. That default does not work here, for two independent
reasons:

**It loses exactly what this agent is built to keep.** The cached schedule, the
cached config, the device credentials and a month of readings all live on disk
so that a boiler room which loses power at 3 a.m. comes back doing the right
thing. In RAM they are gone at every boot, and the reading database grows until
it exhausts memory.

**The build will not fit.** Alpine packages neither `RPi.GPIO` nor `spidev`, and
there are no musl wheels for either, so both are compiled on the device. The
toolchain to do that (`build-base`, `python3-dev`, `linux-headers`) is roughly
250 MB installed — on a diskless Pi Zero W that is 250 MB of a 512 MB RAM disk,
before Python has started.

**So: run a sys install.** The rest of this guide assumes one. Step 1 tells you
which you have and how to convert.

---

## Before you start

- The Pi, powered, with the SD card in it and network reachable over SSH
- The device's provisioning credentials — or not, if you would rather type them
  at the panel, which is now supported
- The relay board, probes, keypad and ST7920 display to hand, but **not yet
  wired to the relays** — see step 2
- Somewhere to run `ssh` and `git`

---

## Step 1 — Find out which Alpine you have

```sh
mount | grep ' / '
```

| Output contains | You have | What to do |
|---|---|---|
| `tmpfs on /` | **Diskless** — root in RAM | Convert, below |
| `/dev/mmcblk0p2 on / type ext4` | **Sys install** | Skip to step 2 |

`lbu status` succeeding is another sign of diskless.

### Converting to a sys install

Two routes. **Reflashing is the more reliable one** if you have nothing on the
card worth keeping — an in-place conversion has to satisfy the Pi's bootloader
at the same time as Alpine's installer, and getting it half-right leaves a card
that does not boot at all.

**Reflash (recommended).** Write the Alpine image to the card, boot it, run
`setup-alpine`, then follow the Alpine wiki's Raspberry Pi page for a sys
install to the card. Keep partition 1 as the FAT boot partition — the Pi's
bootloader reads `config.txt` and the kernel from it and cannot boot without it.

**In place.** Roughly: `apk add e2fsprogs parted`, create an ext4 partition in
the free space after the FAT one, `setup-disk -m sys /dev/mmcblk0p2`, then point
the kernel at it by editing `root=` in `/boot/cmdline.txt`. Check the Alpine
wiki for the current specifics rather than trusting this outline.

### Add swap either way

Compiling two C extensions on a 512 MB Pi Zero W is tight. Give it room:

```sh
dd if=/dev/zero of=/swapfile bs=1M count=512
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

You can `swapoff` and delete it after the install if you would rather not have
swap on an SD card long-term. Leaving it is also defensible on a device with
512 MB.

> **Check:** `mount | grep ' / '` shows an ext4 root, and `free -m` shows swap.

---

## Step 2 — Settle the GPIO conflict before anything is energised

**Do this before you connect the keypad to a powered relay board.** Scanning a
keypad drives its row pins high and low. If a row pin is also a relay pin, then
**pressing a key fires a boiler**.

Right now the two overlap. The server's device record puts the relays on GPIO
17–21; the keypad's default pin map uses 17 as row A and 19 as column 3:

| | Relays (from the record) | Keypad default | Clash |
|---|---|---|---|
| Rows A–D | — | 17, 27, 22, 5 | **17** |
| Cols 1–4 | — | 6, 13, 19, 26 | **19** |
| Relays 1–5 | 17, 18, 19, 20, 21 | — | |

The agent already refuses to start the keypad in this state and says which
relay each clashing pin belongs to. That is a hard refusal, not a warning: it
would rather lose the local menu than switch a boiler on a keypress. Two ways
out, and **which one is right depends on a fact only you can check**:

**A — the record is wrong.** The README documents this installation's relay
wiring as GPIO 18, 23, 24, 25, 12, 16, 20, 21, which is not what the record
says. If the physical relay board is on those pins, the record is simply
incorrect and the agent is currently prepared to drive the wrong pins entirely.
Fix it server-side, and the keypad default may then be fine. **Check the actual
board before deciding anything else** — this is worth ten minutes with a
multimeter.

**B — the record is right.** Then move the keypad, which is two jumpers:

```sh
# row A: GPIO 17 -> 16     column 3: GPIO 19 -> 12
BOILERROOM_KEYPAD_ROWS=16,27,22,5
BOILERROOM_KEYPAD_COLS=6,13,12,26
```

Those two replacements avoid the relays, the SPI bus, the 1-Wire pin, the I²C
pins (which carry fixed pull-ups) and the UART console pins. Put them in `.env`
once the checkout is on the device, at step 4.

> **Check:** every relay GPIO, the four keypad rows and the four keypad columns
> are eight-plus-five distinct pins, none of them 4, 7, 8, 9, 10 or 11.

---

## Step 3 — Wire the display

| ST7920 | Pi | |
|---|---|---|
| SID | GPIO10 | MOSI |
| CLK | GPIO11 | SCLK |
| CS | GPIO7 | CE1 |
| VCC | 5V | |
| GND | GND | |

It shares the SPI bus with the gas ADC on CE0; separate chip selects, so they
coexist. Two things about this controller cause almost every "it stays blank":

- **PSB must be tied LOW** for serial mode. It is a jumper or solder pad on the
  module, not one of the five wires.
- **CS is active HIGH**, unlike every other SPI device on the header. The driver
  sets that; `BOILERROOM_DISPLAY_CS_HIGH=off` is for a module strapped high in
  hardware instead.

One more, worth knowing before you conclude the board is dead: the ST7920 at 5 V
wants about 3.5 V to read a logic high and the Pi drives 3.3 V. It usually
works. If it does not, running the panel's VCC at 3.3 V is the usual fix.

---

## Step 4 — Get the code onto the Pi

```sh
apk add git
cd /opt
git clone https://git.mayanext.com/mehrab_ghw/boilerRoom-edge.git
cd boilerRoom-edge
```

Cloning is worth it over copying: it gets the executable bits right, and it is
how you pull a fix later.

If you copy instead, from your machine:

```sh
rsync -av --exclude .git --exclude data --exclude .env ./ root@boiler-pi:/opt/boilerRoom-edge/
chmod +x deploy/install-alpine.sh          # scp and zip both lose this
```

Now add anything from step 2 to `.env`:

```sh
cp .env.example .env
$EDITOR .env        # keypad pins if you chose B; leave the credentials empty
```

Leaving `BOILERROOM_DEVICE_USERNAME` and `BOILERROOM_DEVICE_PASSWORD` empty is
deliberate — step 7 types them at the panel. If you would rather set them here,
that works too and step 7 is then skipped automatically.

> **Check:** `ls deploy/` shows `install-alpine.sh`, and
> `head -1 deploy/install-alpine.sh` prints `#!/bin/sh` with no stray `\r`.

---

## Step 5 — Run the installer

```sh
doas ./deploy/install-alpine.sh          # or sudo, or as root
```

It does four things, and stops rather than continuing past a failure:

1. Builds a virtualenv at `.venv-pi` with `websockets`, `tzdata`, `RPi.GPIO`
   and `spidev`, pulling in the toolchain to compile the last two. This is the
   slow part — expect several minutes on a Zero W.
2. Appends `dtparam=spi=on` and `dtoverlay=w1-gpio` to the boot config, and
   `spi-bcm2835`, `w1-gpio`, `w1-therm` to `/etc/modules`.
3. Writes `/etc/init.d/boilerroom-edge` and `/etc/conf.d/boilerroom-edge`.
4. `rc-update add … default` and starts it.

`tzdata` matters more than it looks: Alpine ships no timezone database, and
without one the schedule silently falls back to system local time instead of
`Asia/Tehran`.

> **Check:** the installer ends with "Enabled: the agent now starts on boot".
> If it stops on a missing module, the compile failed — see troubleshooting.

---

## Step 6 — Reboot, and confirm the buses came up

The overlays from step 5 need a reboot.

```sh
reboot
```

Then:

```sh
ls -l /dev/spidev0.*          # want spidev0.0 (gas ADC) and spidev0.1 (display)
ls /sys/bus/w1/devices/       # want a 28-xxxxxxxx entry per temperature probe
```

The `28-…` names are the 1-Wire ROM codes. They must match the `physical_id`
values in the server's device record, or the agent reads nothing from probes it
believes exist.

> **Check:** both spidev nodes exist, and you can count your probes.

---

## Step 7 — Sign the device in, at the panel

If you left the credentials empty, the display now shows:

```
+---------------------+
|SIGN IN              |
|Not signed in yet.   |
|                     |
|Type the username    |
|and password from    |
|provisioning.        |
|Digits only.         |
|# Done  * Back       |
+---------------------+
```

Press `#`, type the username, `#`, type the password (shown as dots), `#`.

The keys throughout: **2** up, **8** down, **#** select, **\*** back, **A**
delete.

Nothing is written to `.env` until the server has actually accepted the
credentials — a mistyped digit comes back as "Not accepted" and asks again,
rather than being cached into a device that then cannot be corrected from the
panel. If the server cannot be reached, the credentials are kept and retried in
the background, and saved the moment one gets through.

> **Check:** `grep BOILERROOM_DEVICE_USERNAME .env` shows the username, and
> `data/boilerroom.log` has `[auth] Device session established`.

---

## Step 8 — Verify it end to end

```sh
rc-service boilerroom-edge status
tail -f data/boilerroom.log
```

Lines worth seeing on a healthy boot:

```
[main] Hardware: real sensors, relays, keypad and display
[menu] Display: ST7920 128x64 LCD on SPI 0.1 at 800 kHz, CS active high
[mapping] Mapping built from the cached record: N temperature sensor(s) ...
[auth] Device session established (device_id=DEV-…)
[schedule] Restored cached schedule v… (… weekly rule(s), tz=Asia/Tehran)
[ws] Received device.hello_ack …
[telemetry] Posted N readings …
```

`[main] Hardware: SIMULATED` on that first line means the agent did not
recognise the Pi and is inventing its readings — a device in that state looks
healthy from the server and is measuring nothing. Force it with
`BOILERROOM_MOCK_HARDWARE=off` and find out why detection failed.

Then, from the panel: press `*` from the main menu for the status screen, and
confirm the temperatures match the probes and the relay states match the board.

---

## Step 9 — Pull the power

The test that actually matters, and the one this whole design exists to pass.
With the network unplugged as well, so you are testing the caches and not the
server:

1. Unplug the network.
2. Pull the power.
3. Plug the power back in.

It should come up running the cached schedule, driving relays on programme,
logging `[auth] Login failed … Retrying`, and holding readings in the outbox.
Plug the network back in and the backlog drains.

> **Check:** relays return to their scheduled positions with no network at all,
> and the schedule version in the log matches what it was before the cut.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `bad interpreter: no such file or directory` | The script has CRLF line endings from Windows | `sed -i 's/\r$//' deploy/*.sh` — and check `.gitattributes` came with the clone |
| Compile of RPi.GPIO fails, or the Pi hangs | Out of memory | Add the swap file from step 1; confirm you are not on a diskless install |
| Display stays blank | PSB not tied low; CS not active high; SPI too fast | In that order. Then `BOILERROOM_DISPLAY_SPI_SPEED=400000` |
| No `/dev/spidev0.1` | Overlay not applied | `grep spi /boot/*.txt`, then reboot |
| Every probe reads unavailable | 1-Wire not enabled, or ROM codes do not match the record | `ls /sys/bus/w1/devices/` and compare with `physical_id` in the record |
| Keypad will not start, log names a relay | The step 2 conflict | Fix the record or move the pins |
| Menu never appears, panel shows only status | The keypad failed to start | The display falls back to a rotating status screen by design — fix the keypad |
| Service restarts over and over | Missing Python module, or a crash at startup | `tail -50 /var/log/boilerroom-edge.log` |
| Everything is forgotten after a reboot | Diskless install | Step 1 |
| Schedule runs at the wrong times | No timezone database | `.venv-pi/bin/python -c "import zoneinfo; zoneinfo.ZoneInfo('Asia/Tehran')"` |

---

## What has not been tested

Being straight about this, because it is where your time will go:

- **None of the Alpine path has run on real hardware.** The scripts pass syntax
  checks and the logic is straightforward, but `apk`, `rc-service` and the
  compile have not been executed.
- **The `RPi.GPIO` build on musl is the most likely thing to surprise you.** It
  compiles against Linux headers and normally builds, but Alpine is not its
  usual home.
- **The keypad and display have never been run on real hardware at all**, on any
  distribution. Every frame the panel would be sent has been rendered and
  checked, and the menus driven end to end against a simulated pad — but that
  says nothing about whether the ST7920 accepts the frames, or whether the key
  table matches the caps on your pad. `python src/keypad.py --test` prints what
  each key produces; set `BOILERROOM_KEYPAD_LAYOUT` from what you see.

---

## Day-to-day afterwards

```sh
rc-service boilerroom-edge status          # running?
rc-service boilerroom-edge restart         # after an .env change
tail -f data/boilerroom.log                # what the agent says
tail -f /var/log/boilerroom-edge.log       # what OpenRC saw
rc-update del boilerroom-edge default      # stop starting at boot
```

Paths and the interpreter are in `/etc/conf.d/boilerroom-edge`. To take an
update: `git pull`, then restart the service — and re-run the installer instead
if `requirements*.txt` changed.
