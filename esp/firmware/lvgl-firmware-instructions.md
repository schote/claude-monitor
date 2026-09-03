# LVGL MicroPython firmware — reference for future agents

Everything needed to write an application against the custom firmware
flashed on this project's board. All values below were read out of the
actual build tree and the compiled binary on 2026-08-29, not from upstream
documentation.

---

## 1. Exact target

| | |
|---|---|
| Dev board | **ESP32-2424S012C** (1.28" round touch LCD module) |
| SoC | **ESP32-C3** — RISC-V, single core, 4 MB flash, **no PSRAM** |
| MicroPython board target | `ESP32_GENERIC_C3` |
| Display controller | **GC9A01**, 240×240, SPI |
| Touch controller | **CST816** (CST816S driver), I²C addr `0x15` |
| Serial port (this Mac) | `/dev/cu.usbmodem14301` (native USB-Serial-JTAG) |

This firmware is **only** valid for an ESP32-C3. It will not boot on an
ESP32/S2/S3 — those need a rebuild with a different `BOARD=`.

---

## 2. Firmware identity

| | |
|---|---|
| Built from | [`lvgl_micropython`](https://github.com/lvgl-micropython/lvgl_micropython) @ `d2d26467fa4cb9e99e569d899709043d086f7a6f` (2026-06-23) |
| MicroPython | 1.27.0 — submodule `78ff170de9e32c79db6e64d3e33d2bd60002bdcd` |
| LVGL | v9.4.0 — submodule `c016f72d4c125098287be5e83c0f1abed4706ee5` |
| ESP-IDF | v5.5.1 — submodule `fcae32885b0296b32044cb99ecbdc50d98dddb83` |
| Image (in repo) | `firmware/lvgl-micropython-esp32c3-gc9a01-cst816s.bin` |
| Size / MD5 | 3,291,232 B / `c61ad83d26265b3785b495ee529523e7` |
| Build clone | `~/Projects/esp32/lvgl_micropython` (outside this repo) |

### Flash layout (`build/partitions.csv`, 4 MB flash)

```
nvs       data nvs     0x9000   0x6000
phy_init  data phy     0xF000   0x1000
factory   app  factory 0x10000  0x314000   (3152 KiB)
vfs       data fat     0x324000 0xDC000    ( 880 KiB)
```

`firmware.bin` is a **combined** image (bootloader + partition table + app)
— flash it at offset `0x0`, not `0x10000`.

---

## 3. How it was obtained (reconstruction)

```sh
conda create -p ~/Projects/envs/esp-idf-builder -c conda-forge python cmake ninja
conda activate ~/Projects/envs/esp-idf-builder          # activate BEFORE building

git clone https://github.com/lvgl-micropython/lvgl_micropython.git
cd lvgl_micropython && git checkout d2d2646

python3 make.py esp32 BOARD=ESP32_GENERIC_C3 DISPLAY=gc9a01 INDEV=cst816s
```

`make.py` pulls its own ESP-IDF and RISC-V toolchain into `~/.espressif`.
First build ≈ 5 GB, 20–40 min. No Homebrew involved; cmake/ninja come from
conda-forge. `DISPLAY=`/`INDEV=` decide which drivers get frozen in.

**Build rules that matter** (each of these cost an hour once):

- Run **exactly one** build at a time. Two concurrent `make.py` runs share
  `build-ESP32_GENERIC_C3/` and wipe each other, producing fake errors
  (`opening dependency file …obj.d: No such file`, `error performing the
  clean`). Recover: `pgrep -fl ninja`, then `rm -rf` that build dir.
- **Never** run `make.py … submodules` standalone — it configures into an
  extra `build-*/submodules/` level where the relative `USER_C_MODULES`
  path resolves one directory too high and CMake wrongly reports
  `USER_C_MODULES doesn't exist`. The full build does submodules itself.
  (Seeing `Running cmake in directory …/build-*/submodules` *inside* a
  normal build is fine — that is its first phase.)
- ESP-IDF's venv (`~/.espressif/python_env/`) symlinks `bin/python3` at the
  conda env that seeded it. Rename or delete that env and the symlink
  dangles; `venv` will not repair an existing directory, so IDF dies with
  `[Errno 2] … idf5.5_py3.13_env/bin/python3`. Fix: `rm -rf
  ~/.espressif/python_env`.
- ESP-IDF v5.5.1 accepts Python 3.9–3.13 (built with 3.13.5). Do **not**
  put conda `compilers`/`gcc`/`binutils` in the build env — they shadow the
  RISC-V toolchain.

Rebuilding is a **one-time job** — the resulting image is committed at
`firmware/`, so normal work never touches the build tree. Flash and upload
commands are in [`README.md`](README.md).

Two esptool generations exist on this machine and their CLIs differ:
`esptool 5.3.1` in the `esp-idf-builder` conda env uses **hyphens**
(`write-flash`), while the `esptool 4.12.0` inside ESP-IDF's venv
(`~/.espressif/python_env/idf5.5_py3.13_env/bin/esptool.py`) uses
**underscores** (`write_flash`). Match the syntax to whichever is invoked.

---

## 4. What is in this firmware

**Frozen Python modules** (verified in `frozen_content.c` / the binary):

`lvgl` · `lcd_bus` · `gc9a01` · `cst816s` · `i2c` · `task_handler` ·
`display_driver_framework` · `pointer_framework` · `touch_cal_data`

Plus normal MicroPython: `machine`, `network`, `socket`, `framebuf`,
`espnow`, `neopixel`, `micropython`.

Only the **gc9a01** display driver and **cst816s** indev driver are
included. Any other panel/touch controller needs a rebuild.

**Fonts compiled in** (confirmed by `strings` on the binary):
Montserrat **12, 14, 16, 18, 28, 40**. Default is 14. Other sizes require
editing `lib/lv_conf.h` (note: `lib/lv_conf.h`, *not* `lib/lvgl/lv_conf.h`,
which does not exist) and rebuilding.

**LVGL config highlights** — `LV_USE_OS = LV_OS_NONE`,
`LV_DEF_REFR_PERIOD = 33 ms`, `LV_USE_LOG = 0`, `LV_COLOR_DEPTH` and
`LV_MEM_SIZE` bound to MicroPython's values.

---

## 5. Verified bring-up template

This exact sequence works on this board. Copy it as the header of any app.

```python
import time, machine
import lcd_bus, lvgl as lv, gc9a01, i2c, cst816s, task_handler

# --- pins (ESP32-2424S012C) ---
_SCK, _MOSI, _MISO, _HOST = 6, 7, -1, 1     # host 0 is reserved for flash
_LCD_CS, _DC, _BL = 10, 2, 3
_TP_SDA, _TP_SCL, _TP_RST, _TP_ADDR = 4, 5, 1, 0x15

# --- display: TWO bus objects, this is the #1 gotcha ---
spi_bus = machine.SPI.Bus(host=_HOST, mosi=_MOSI, miso=_MISO, sck=_SCK)
display_bus = lcd_bus.SPIBus(spi_bus=spi_bus, freq=40_000_000,
                             dc=_DC, cs=_LCD_CS)

display = gc9a01.GC9A01(
    data_bus=display_bus,
    display_width=240, display_height=240,
    backlight_pin=_BL, backlight_on_state=gc9a01.STATE_HIGH,
    color_space=lv.COLOR_FORMAT.RGB565,
    color_byte_order=gc9a01.BYTE_ORDER_BGR,
    rgb565_byte_swap=True,
)                       # omit frame_buffer1/2 -> driver sizes them itself

display.set_power(True)
display.init()
display.set_backlight(100)
display.set_rotation(lv.DISPLAY_ROTATION._0)

# --- touch ---
i2c_bus = i2c.I2C.Bus(host=0, scl=_TP_SCL, sda=_TP_SDA, freq=400_000)
touch_dev = i2c.I2C.Device(bus=i2c_bus, dev_id=_TP_ADDR, reg_bits=8)
indev = cst816s.CST816S(touch_dev, reset_pin=_TP_RST)

# --- build UI on lv.screen_active() here ---

th = task_handler.TaskHandler()   # pumps lv.timer_handler(); keep a reference
```

### Non-obvious API facts

- `lcd_bus.SPIBus` takes **`spi_bus=`**, plus `dc`/`cs`/`freq` only. It has
  no `host`/`sclk`/`mosi`/`miso`; passing those raises
  `TypeError: 'spi_bus' argument required`.
- **Framebuffers are optional** — omit `frame_buffer1`/`frame_buffer2` and
  the driver picks a partial-buffer size that fits. Preferred on this
  PSRAM-less chip over hard-coding a size that may fail to allocate.
- Bring-up order is `set_power(True)` → `init()` → `set_backlight(100)`.
- **There is no `invert_colors()`.** The GC9A01 init sequence already
  sends `INVON` (`_gc9a01_init.py`), so black renders black.
- `cst816s.CST816S(device, reset_pin=…)` performs its own reset pulse —
  do not toggle the pin manually.
- Touch uses the frozen **`i2c`** module (`i2c.I2C.Bus` / `i2c.I2C.Device`),
  **not** `machine.I2C`.
- `lv.init()` is unnecessary — `display_driver_framework` calls it.
- Keep a reference to the `TaskHandler`; if it is garbage collected the UI
  stops updating.
- If text appears mirrored, try `lv.DISPLAY_ROTATION._90/._180/._270`. If
  colours are swapped, use `gc9a01.BYTE_ORDER_RGB`.
- SPI at 40 MHz is stable here; drop to 27 MHz if artifacts appear.

### Touch input: how the stack actually fits together

The panel is *not* read by LVGL directly. The chain is

```
CST816S._get_coords()          # cst816s.py — I2C regs 0x02..0x06
  -> PointerDriver._read()     # pointer_framework.py — calibration, debug print
    -> lv_indev read timer     # created by lv.indev_create(), INDEV_MODE.TIMER
      -> lv.task_handler()     # pumped by TaskHandler's machine.Timer
        -> hit test -> widget events (PRESSED / PRESSING / RELEASED / GESTURE)
```

Consequences worth knowing before debugging an unresponsive app:

- **`debug=True` is the built-in probe.** `cst816s.CST816S(dev,
  reset_pin=1, debug=True)` makes `PointerDriver._read` print
  `CST816S(raw_x=…, raw_y=…, x=…, y=…, state=PRESSED)` on every coordinate
  change. If those lines appear but your widget never fires, the panel is
  fine and the bug is in the UI (hit test, flags, callback lifetime). If
  they never appear, nothing above the driver can help.
- **A press only reaches a widget that is `CLICKABLE`.** Objects without
  the flag are skipped by the hit test entirely. Prefer an explicit
  transparent full-screen child as the event target over the screen object
  — it has no scroll/layer behaviour of its own and cannot be shadowed by
  a decoration. Non-interactive decorations should have `CLICKABLE`
  *removed* so they never swallow a press.
- **Keep a Python reference to every event callback.** A lambda passed
  straight to `add_event_cb` can be collected, after which the event
  silently stops firing — the same failure mode as a collected
  `TaskHandler`, with no error either.
- **Coordinates: `indev.get_point(pt)` on the driver object you already
  hold.** `PointerDriver` forwards it to `lv_indev_get_point`, which
  returns the last known point at any time. That beats rediscovering the
  indev through `e.get_indev()` / `lv.indev_active()` inside a callback.
  `get_gesture_dir()`, `get_vect()` and `get_scroll_dir()` are forwarded
  the same way, so LVGL's own swipe verdict is available as
  `indev.get_gesture_dir()` against `lv.DIR.LEFT` / `.RIGHT`.
- **Without calibration data the raw chip coordinates are passed through
  unchanged** (`_calc_coords` only rotates per `startup_rotation`).
  `indev.is_calibrated` reports it; `indev.calibrate()` runs the 3-point
  routine from `touch_calibrate.py` and `self._cal.save()` persists it.
  So a rotated or mirrored *display* does not rotate touch — pass the same
  rotation as `startup_rotation=` to the driver.
- **`TaskHandler` swallows and then kills.** Its default exception hook
  prints the traceback *and calls `deinit()`*, stopping the timer. So an
  exception raised inside any LVGL callback shows up as "the UI froze
  after I touched it", and a UI that keeps animating proves your callbacks
  are not raising — they are simply not being called.

[`apps/touch-test/main.py`](../apps/touch-test/main.py) walks these three
layers in order (I2C scan → driver coordinates → widget events) and shows
the counters on screen; upload it whenever touch behaves oddly.

### Enum names are LVGL **9.4** names — verify, don't recall

The binding follows the C names of *this* LVGL version, and 9.3 renamed a
number of enums. Guessing from older tutorials (or from LVGL 8) produces
`AttributeError: type object 'label' has no attribute 'LONG'` at *build*
time of the UI, i.e. only when that line runs.

Confirmed on this firmware:

| Use | Correct in 9.4 | Wrong (8.x / pre-9.3) |
|---|---|---|
| Label truncation | `lv.label.LONG_MODE.DOTS` | `lv.label.LONG.DOT` |
| Other modes | `LONG_MODE.WRAP` / `.CLIP` / `.SCROLL` / `.SCROLL_CIRCULAR` | `LONG.WRAP` etc. |
| Clear an object flag | `obj.remove_flag(...)` | `obj.clear_flag(...)` (still present) |
| Active screen | `lv.screen_active()` | `lv.scr_act()` |

Two ways to settle it without a build tree:

```python
# on the device, over the REPL — authoritative
import lvgl as lv
print([a for a in dir(lv.label) if 'LONG' in a])   # -> ['LONG_MODE']
print([a for a in dir(lv.label.LONG_MODE)])        # -> DOTS, CLIP, SCROLL, ...
```

```sh
# from the repo, against the committed image — qstr names are stored plain
strings -n 4 firmware/lvgl-micropython-esp32c3-gc9a01-cst816s.bin | grep -x DOTS
```

The `strings` check is only trustworthy for **hits**: a name that shows up
exists, but absence proves nothing (`screen_active` does not appear yet
works fine). For a negative answer, use `dir()` on the device.

Where an app can plausibly run on more than one binding revision, resolve
the enum once at import with `getattr` fallbacks and degrade instead of
raising — see `_resolve_long_dots()` in
[`apps/birthday-reminder/main.py`](../apps/birthday-reminder/main.py).

---

## 6. Memory budget (the real constraint)

The C3 is single core with ~400 KB SRAM and **no PSRAM**. After boot,
roughly 100–160 KB of MicroPython heap is free, and LVGL's own arena comes
out of that.

- A full 240×240×16bpp framebuffer is 115 KB and **will not allocate** —
  this is why the pure-Python driver in `src/` renders in bands and why
  LVGL must use partial buffers.
- Update existing widgets instead of rebuilding the tree each frame.
- Check headroom: `import gc; gc.collect(); print(gc.mem_free())`.
- On `MemoryError` during `allocate_framebuffer` (only if buffers are
  passed explicitly), shrink the buffer or pass a single one.

If an app outgrows this, an **ESP32-S3 round board with PSRAM** runs the
same source with full double framebuffers — requires rebuilding with the
matching `BOARD=`.

---

## 7. Ground truth in the source tree

When an API is uncertain, read these rather than guessing — they are on
disk at `~/Projects/esp32/lvgl_micropython`, **if that clone still
exists**. It is outside this repo and was absent on 2026-08-29; without
it, `dir()` over the REPL is the fallback ground truth (§5).

| Question | File |
|---|---|
| Bus / display / touch signatures | `stubs/lcd_bus.pyi`, `stubs/i2c.pyi`, `stubs/display_driver_framework.pyi` |
| Full LVGL 9.4 Python API | `lvgl.pyi` (~15k lines) |
| Worked end-to-end example | `README.md` — search for `SPIBus(` |
| GC9A01 init / MADCTL table | `api_drivers/common_api_drivers/display/gc9a01/` |
| CST816S driver | `api_drivers/common_api_drivers/indev/cst816s.py` |
| Enabled fonts & LVGL options | `lib/lv_conf.h` |

Without that clone, the frozen driver sources come from GitHub at the
pinned commit — the paths are not where they look like they should be
(`pointer_framework.py` is under `py_api_drivers`, not
`common_api_drivers`, and guessing gets a 404):

```sh
C=d2d26467fa4cb9e99e569d899709043d086f7a6f
R=https://raw.githubusercontent.com/lvgl-micropython/lvgl_micropython/$C
curl -sO $R/api_drivers/common_api_drivers/indev/cst816s.py
curl -sO $R/api_drivers/py_api_drivers/frozen/indev/pointer_framework.py
curl -sO $R/api_drivers/py_api_drivers/frozen/indev/_indev_base.py
curl -sO $R/api_drivers/common_api_drivers/frozen/other/task_handler.py
# list every path in the commit:
curl -s "https://api.github.com/repos/lvgl-micropython/lvgl_micropython/git/trees/$C?recursive=1"
```

These are the *exact* sources frozen into the committed image, so they are
as authoritative as the build tree was.

---

## 8. Runtime troubleshooting

| Symptom | Cause / fix |
|---|---|
| `TypeError: 'spi_bus' argument required` | `lcd_bus.SPIBus` needs a `machine.SPI.Bus` object, not pin numbers (§5) |
| `AttributeError: … 'invert_colors'` | no such method; the GC9A01 init already sends `INVON` |
| Panel stays dark | `backlight_pin=3`, `backlight_on_state=gc9a01.STATE_HIGH`, and `set_backlight(100)` after `init()` |
| Text mirrored | try `lv.DISPLAY_ROTATION._90/._180/._270` |
| Colours swapped | `color_byte_order=gc9a01.BYTE_ORDER_RGB` |
| SPI sparkle / torn frames | lower `freq` on `lcd_bus.SPIBus` to 27 MHz or 20 MHz |
| Touch dead | `i2c_bus.scan()` should list `0x15`; pass `reset_pin=1` to `CST816S` |
| UI frozen, no error | the `TaskHandler` was garbage collected — keep a module-level reference |
| `MemoryError` on start | do not pass explicit framebuffers; let the driver allocate |
| `AttributeError: type object 'label' has no attribute 'LONG'` | 9.3 enum rename — use `lv.label.LONG_MODE.DOTS`; check any enum with `dir()` on the device (§5) |
| `AttributeError` on some other LVGL enum/method | same cause: an 8.x or pre-9.3 name. `dir(lv.<widget>)` in the REPL is the ground truth (§5) |
| Touch does nothing, no error, UI still animates | callbacks are not being *called*: target lacks `CLICKABLE`, or the callback was garbage collected. Re-run with `debug=True` on the driver to see whether coordinates arrive at all (§5) |
| UI freezes the moment you touch it | an exception in an event callback — `TaskHandler`'s default hook prints it and then `deinit()`s the timer (§5) |
| Touch coordinates rotated vs. the display | `display.set_rotation()` does not rotate input; pass `startup_rotation=` to the touch driver (§5) |
| `port is busy or doesn't exist` | MicroPico/VS Code or an open REPL owns the port |
| Upload from MicroPico/mpremote hangs or fails, port *not* busy | the running app's `machine.Timer` + prints are talking over the raw REPL — see below |

### A running app blocks uploads

Uploading is not a separate channel: MicroPico and `mpremote` drive the
**raw REPL over the same UART** the app prints to. An LVGL app leaves the
board permanently busy — `TaskHandler` arms a `machine.Timer` at 33 ms
which `micropython.schedule()`s `lv.task_handler()` forever, and the app's
LVGL timers run on top. Two things then break the upload handshake:

1. **Stray output.** Anything printed from a scheduled callback lands in
   the middle of the raw-REPL/raw-paste exchange and the host sees a
   corrupt reply. `debug=True` on the touch driver is the worst offender —
   it prints on *every* coordinate change, so merely resting a finger on
   the panel during an upload breaks it. Ship apps with debug off.
2. **Timing.** Raw-paste mode is flow-controlled and the host times out;
   a callback running every 33 ms eats into that budget.

This is why an app that ran fine yesterday starts blocking uploads today —
nothing about the tooling changed, the board just never goes idle.

Fixes, in order of preference:

- **Give every app a boot escape hatch** as its first statements, before
  any hardware is claimed:

  ```python
  BOOT_DELAY_S = 2
  try:
      print("app: Ctrl-C within %ds to stay in the REPL" % BOOT_DELAY_S)
      time.sleep(BOOT_DELAY_S)
  except KeyboardInterrupt:
      raise SystemExit          # no timers exist yet; REPL is idle and quiet
  ```

  Both apps in this repo start with this block.
- **Stop the timer on a board that is already running.** `main.py` executes
  in `__main__`, so its globals survive at the REPL: `th.deinit()` kills
  the `TaskHandler` timer. `tick_timer.delete()` for an app timer.
- **Last resort:** hold **BOOT**, tap **RST**, release **BOOT**. The board
  comes up without running `main.py` at all.

---

## 9. Related files in this repo

- [`apps/`](../apps) — one directory per app, each with a `main.py` whose
  header block is the verified bring-up sequence; copy from any of them.
  [`apps/touch-test/main.py`](../apps/touch-test/main.py) is the input
  diagnostic described in §5.
- [`README.md`](README.md) — developer-facing quickstart: tooling, flash,
  upload, and the limitations to design around.
- `firmware/` — the flashable image described in §2.

A pure-Python (non-LVGL) driver set for **stock** MicroPython previously
lived in `src/gc9a01.py` and `src/cst816.py`. It was removed once the LVGL
firmware worked; recover it from git history if a stock-firmware fallback
is ever needed. It rendered in horizontal bands because a full framebuffer
does not fit (§6).
