# Claude usage monitor for the ESP32-2424S012C, built on LVGL 9.
#
#   ESP32-C3 + GC9A01 240x240 round LCD + CST816 touch
#
# Shows the 5-hour and weekly Claude usage served by the daemon in
# daemon/usage_daemon.py (a Raspberry Pi on the same LAN), which reads the
# real account-wide figures out of `claude -p "/usage"`. Tap to swap which
# of the two rings is the big number.
#
# Requires the custom LVGL firmware in ../../firmware/ - see ../../README.md
# for flashing and upload, ../../firmware/lvgl-firmware-instructions.md for
# the full API.
#
# Copy WIFI_SSID / WIFI_PASSWORD / USAGE_URL into a config.py next to this
# file (see config.example.py) so credentials stay out of git; the defaults
# below are only a fallback.
#
# Staying up is the whole job here, so recovery is layered (see "Keeping it
# alive" below): cached DNS, fast retries, Wi-Fi re-association, a reboot
# when nothing has worked for a while, and a hardware watchdog underneath
# all of it in case the UI thread itself dies.

import time

# ---------------------------------------------------------------------------
# Boot escape hatch - must come before any hardware is claimed, otherwise a
# running app leaves the board too busy to accept the next upload
# (firmware instructions, section 8). It also runs before the watchdog is
# armed, so Ctrl-C here is still the way back to a usable REPL.
# ---------------------------------------------------------------------------

BOOT_DELAY_S = 2
try:
    print("claude-usage-monitor: Ctrl-C within %ds to stay in the REPL" % BOOT_DELAY_S)
    time.sleep(BOOT_DELAY_S)
except KeyboardInterrupt:
    raise SystemExit

import gc
import json
import socket

import machine
import network

import lcd_bus
import lvgl as lv
import gc9a01
import i2c
import cst816s
import task_handler


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WIFI_SSID = ""
WIFI_PASSWORD = ""
USAGE_URL = "http://rpi:8000/usage"
USAGE_URLS = ()               # optional fallbacks, tried in order after the first

FETCH_INTERVAL_S = 120        # how often to ask the daemon when all is well
HTTP_TIMEOUT_S = 5            # the fetch blocks the UI for at most this long
WIFI_TIMEOUT_S = 20           # give an association this long before retrying

# ---------------------------------------------------------------------------
# Keeping it alive
#
# A display that silently stops updating is worse than one that reboots, so
# every failure mode has an escape, ordered cheapest first:
#
#   1. retry sooner than the normal interval, backing off      RETRY_*
#   2. drop the cached IP, in case the Pi moved                FAILS_BEFORE_REDNS
#   3. tear the Wi-Fi association down and rebuild it          FAILS_BEFORE_WIFI_RESET
#   4. reboot the board                                        REBOOT_AFTER_S
#   5. hardware watchdog, for when the UI thread itself dies   WATCHDOG_S
#
# 1-4 are decisions the app makes; 5 catches the case where the app can no
# longer make any decision at all.
# ---------------------------------------------------------------------------

RETRY_MIN_S = 10              # first retry after a failed fetch
RETRY_MAX_S = 120             # ceiling for the exponential backoff
FAILS_BEFORE_REDNS = 2        # forget the cached IP after this many failures
FAILS_BEFORE_WIFI_RESET = 4   # full re-association after this many
REBOOT_AFTER_S = 15 * 60      # no successful fetch for this long -> reset
WATCHDOG_S = 30               # UI thread must feed the WDT at least this often
WATCHDOG = True               # set False in config.py while developing
WIFI_POWER_SAVE = False       # True lets the radio doze between beacons

try:
    import config

    WIFI_SSID = getattr(config, "WIFI_SSID", WIFI_SSID)
    WIFI_PASSWORD = getattr(config, "WIFI_PASSWORD", WIFI_PASSWORD)
    USAGE_URL = getattr(config, "USAGE_URL", USAGE_URL)
    USAGE_URLS = getattr(config, "USAGE_URLS", USAGE_URLS)
    FETCH_INTERVAL_S = getattr(config, "FETCH_INTERVAL_S", FETCH_INTERVAL_S)
    REBOOT_AFTER_S = getattr(config, "REBOOT_AFTER_S", REBOOT_AFTER_S)
    WATCHDOG = getattr(config, "WATCHDOG", WATCHDOG)
    WIFI_POWER_SAVE = getattr(config, "WIFI_POWER_SAVE", WIFI_POWER_SAVE)
except ImportError:
    print("no config.py - using built-in defaults")


# ---------------------------------------------------------------------------
# Pins (ESP32-2424S012C)
# ---------------------------------------------------------------------------

_SCK = const(6)
_MOSI = const(7)
_MISO = const(-1)        # panel is write-only
_HOST = const(1)         # SPI2 - host 0 is reserved for flash/SPIRAM

_LCD_CS = const(10)
_DC = const(2)
_BL = const(3)
_LCD_FREQ = const(40_000_000)

_TP_SDA = const(4)
_TP_SCL = const(5)
_TP_RST = const(1)
_TP_ADDR = const(0x15)

_WIDTH = const(240)
_HEIGHT = const(240)


# ---------------------------------------------------------------------------
# Display
#
# The SPI *bus* and the display *bus* are two separate objects:
# machine.SPI.Bus owns the pins, lcd_bus.SPIBus adds dc/cs/freq on top.
# ---------------------------------------------------------------------------

spi_bus = machine.SPI.Bus(
    host=_HOST,
    mosi=_MOSI,
    miso=_MISO,
    sck=_SCK,
)

display_bus = lcd_bus.SPIBus(
    spi_bus=spi_bus,
    freq=_LCD_FREQ,
    dc=_DC,
    cs=_LCD_CS,
)

# No frame_buffer1/2 given on purpose: the driver picks a partial-buffer
# size and allocation that fits. The C3 has no PSRAM, so letting it decide
# beats hard-coding a size that may not allocate.
display = gc9a01.GC9A01(
    data_bus=display_bus,
    display_width=_WIDTH,
    display_height=_HEIGHT,
    backlight_pin=_BL,
    backlight_on_state=gc9a01.STATE_HIGH,
    color_space=lv.COLOR_FORMAT.RGB565,
    color_byte_order=gc9a01.BYTE_ORDER_BGR,
    rgb565_byte_swap=True,
)

display.set_power(True)
display.init()
display.set_backlight(100)
display.set_rotation(lv.DISPLAY_ROTATION._0)


# ---------------------------------------------------------------------------
# Touch
#
# CST816S handles its own reset pulse when given reset_pin. Leave debug off:
# it prints on every coordinate change, which corrupts uploads.
# ---------------------------------------------------------------------------

i2c_bus = i2c.I2C.Bus(host=0, scl=_TP_SCL, sda=_TP_SDA, freq=400_000)
touch_dev = i2c.I2C.Device(bus=i2c_bus, dev_id=_TP_ADDR, reg_bits=8)
indev = cst816s.CST816S(touch_dev, reset_pin=_TP_RST)


# ---------------------------------------------------------------------------
# Fonts - fall back gracefully if a size was not compiled into lv_conf.h
# ---------------------------------------------------------------------------

def _font(*names):
    for n in names:
        f = getattr(lv, "font_montserrat_" + str(n), None)
        if f is not None:
            return f
    return lv.font_montserrat_14


FONT_BIG = _font(40, 36, 28, 24, 14)
FONT_MED = _font(18, 16, 14)
FONT_SMALL = _font(14)


# ---------------------------------------------------------------------------
# State
#
# The daemon sends *seconds remaining* rather than wall-clock reset times,
# so the board never needs its clock set. Between fetches the countdown is
# advanced locally from ticks_ms.
# ---------------------------------------------------------------------------

five_hour_usage = 0
weekly_usage = 0
five_hour_reset_s = -1     # -1 = the daemon could not read a reset time
weekly_reset_s = -1
data_age_s = 0             # how old the daemon's own reading is

show_five_hour = True
_dots = 0                  # animates the "connecting..." message

have_data = False
status = "wifi"                 # wifi | fetch | ok | err
last_ok_ticks = None            # ticks_ms of the last successful fetch
data_ticks = None               # ticks_ms the countdowns were valid at
next_fetch_ticks = 0
fail_count = 0                  # consecutive failed fetches

BLUE = lv.color_hex(0x468CFF)
PURPLE = lv.color_hex(0xB478FF)
WHITE = lv.color_hex(0xF0F2F8)
GRAY = lv.color_hex(0x6E7482)
AMBER = lv.color_hex(0xE0A030)
GREEN = lv.color_hex(0x3CC46E)
RED = lv.color_hex(0xE05252)
TRACK = lv.color_hex(0x181A22)

STALE_AFTER_S = 3 * FETCH_INTERVAL_S   # dim the dot to amber after 3 misses


# ---------------------------------------------------------------------------
# Networking
#
# No urequests in this firmware - only raw sockets - so here is the smallest
# HTTP/1.0 GET that does the job. HTTP/1.0 without keep-alive means the
# server closes the connection and "read until EOF" is a valid body read.
# ---------------------------------------------------------------------------

def _split_url(url):
    if url.startswith("http://"):
        url = url[7:]
    elif url.startswith("https://"):
        raise ValueError("https is not supported - use plain http on the LAN")
    slash = url.find("/")
    hostport, path = (url[:slash], url[slash:]) if slash >= 0 else (url, "/")
    colon = hostport.find(":")
    if colon >= 0:
        return hostport[:colon], int(hostport[colon + 1:]), path
    return hostport, 80, path


# ---------------------------------------------------------------------------
# Where the daemon is
#
# USAGE_URL is the address that should normally work; USAGE_URLS lists any
# others worth trying - a second interface on the Pi, or its name as a last
# resort if the address it was given ever changes. They are tried in order
# and the one that answers is remembered, so the extra candidates cost a
# round trip only while the first choice is actually broken.
#
# Resolving a *name* on every fetch would make the display depend on the
# router's DNS answering at that exact moment; worse, a router hands out a
# stale record for a while after a machine moves from Wi-Fi to a cable. So
# each address is resolved once and cached, and a literal IP - which skips
# resolution altogether - is the sturdiest thing to put here.
# ---------------------------------------------------------------------------

def _target(url):
    try:
        host, port, path = _split_url(url)
    except ValueError:
        host, port, path = url, 80, "/"
    return [host, port, path, None]         # [host, port, path, cached addr]


TARGETS = [_target(u) for u in ([USAGE_URL] + list(USAGE_URLS)) if u]
_target_i = 0                               # the candidate that last worked

def _forget_addresses():
    """Drop every cached lookup - the next fetch resolves from scratch."""
    for t in TARGETS:
        t[3] = None


def _next_target():
    """Rotate to the next candidate after a failure."""
    global _target_i
    if len(TARGETS) > 1:
        _target_i = (_target_i + 1) % len(TARGETS)
        print("trying", TARGETS[_target_i][0])


def http_get_json(timeout=HTTP_TIMEOUT_S):
    t = TARGETS[_target_i]
    host, port, path = t[0], t[1], t[2]
    if t[3] is None:
        t[3] = socket.getaddrinfo(host, port)[0][-1]
    s = socket.socket()
    try:
        s.settimeout(timeout)
        s.connect(t[3])
        req = "GET " + path + " HTTP/1.0\r\nHost: " + host + \
              "\r\nConnection: close\r\n\r\n"
        s.send(req.encode())
        chunks = []
        while True:
            b = s.recv(256)
            if not b:
                break
            chunks.append(b)
        raw = b"".join(chunks)
    finally:
        s.close()

    head, _, body = raw.partition(b"\r\n\r\n")
    first = head.split(b"\r\n", 1)[0].split(b" ")
    if len(first) < 2 or first[1] != b"200":
        raise OSError("HTTP %s" % (first[1].decode() if len(first) > 1 else "?"))
    return json.loads(body)


# ---------------------------------------------------------------------------
# Wi-Fi
#
# isconnected() is not enough on its own: the board can hold an association
# that the access point has already forgotten, and it keeps saying True
# while every socket times out. Having an IP is the weaker claim that is
# actually checked before each fetch; the real proof is a fetch succeeding,
# which is why the recovery ladder is driven by consecutive failures.
# ---------------------------------------------------------------------------

wlan = network.WLAN(network.STA_IF)
wlan.active(True)
_wifi_started = None


def _wifi_tune():
    """Wi-Fi power save is the usual reason a station goes quietly deaf."""
    if WIFI_POWER_SAVE:
        return
    for value in (getattr(network.WLAN, "PM_NONE", None), 0):
        if value is None:
            continue
        try:
            wlan.config(pm=value)
            return
        except (OSError, ValueError):
            pass


_wifi_tune()


def _wifi_ok():
    try:
        return wlan.isconnected() and wlan.ifconfig()[0] not in ("0.0.0.0", "")
    except OSError:
        return False


def _wifi_reset():
    """Tear the radio down and back up - more than disconnect/connect does."""
    global _wifi_started, status, next_fetch_ticks
    _forget_addresses()
    try:
        wlan.disconnect()
    except OSError:
        pass
    try:
        wlan.active(False)
        wlan.active(True)
        _wifi_tune()
    except OSError:
        pass
    try:
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    except OSError as e:
        print("wifi connect failed:", e)
    _wifi_started = time.ticks_ms()
    # Point the on-screen countdown at the next association attempt, so it
    # keeps running - and keeps meaning something - before there is any
    # Wi-Fi to fetch over.
    next_fetch_ticks = time.ticks_add(_wifi_started, WIFI_TIMEOUT_S * 1000)
    status = "wifi"


def _pump_wifi():
    """Non-blocking association: called from a timer until connected."""
    global _wifi_started, next_fetch_ticks
    if _wifi_ok():
        if _wifi_started is not None:
            # Just associated. Fetch now instead of waiting out the
            # association countdown, and arm _wifi_started so that the next
            # drop is acted on at once rather than one timeout later.
            next_fetch_ticks = time.ticks_ms()
            _wifi_started = None
        return True
    now = time.ticks_ms()
    if _wifi_started is None or \
            time.ticks_diff(now, _wifi_started) > WIFI_TIMEOUT_S * 1000:
        _wifi_reset()
    return False


def fetch():
    """One blocking request. Short timeout: this runs inside an LVGL timer."""
    global five_hour_usage, weekly_usage, five_hour_reset_s, weekly_reset_s
    global have_data, status, last_ok_ticks, data_ticks, data_age_s, fail_count

    d = http_get_json()
    five_hour_usage = int(d.get("five_hour_pct", 0))
    weekly_usage = int(d.get("weekly_pct", 0))
    five_hour_reset_s = int(d.get("five_hour_reset_s", -1))
    weekly_reset_s = int(d.get("weekly_reset_s", -1))
    data_age_s = int(d.get("age_s", 0))
    data_ticks = time.ticks_ms()
    last_ok_ticks = data_ticks
    have_data = True
    fail_count = 0
    status = "ok"


_boot_ticks = time.ticks_ms()


def _maybe_reboot(reason):
    """Last rung of the ladder: nothing has worked for REBOOT_AFTER_S.

    Measured from the last good fetch, or from boot if there has never been
    one - a board that came up while the Pi was still booting, or before the
    router had a DNS entry for it, is exactly the case a restart fixes.
    """
    since = last_ok_ticks if last_ok_ticks is not None else _boot_ticks
    if time.ticks_diff(time.ticks_ms(), since) > REBOOT_AFTER_S * 1000:
        print("%s for %ds - resetting" % (reason, REBOOT_AFTER_S))
        time.sleep(1)          # let the print reach the console
        machine.reset()


def _note_failure(exc):
    """Climb the recovery ladder one rung per consecutive failure."""
    global fail_count, status
    fail_count += 1
    status = "err"
    print("fetch failed (%d):" % fail_count, exc)

    # Whatever this candidate's cached address was, it is not working, so
    # give the next one a turn - with a single candidate this is just a
    # re-resolve, which covers a Pi that has moved or a name that was looked
    # up before the Pi had finished booting.
    if fail_count >= FAILS_BEFORE_REDNS:
        TARGETS[_target_i][3] = None
        _next_target()
    if fail_count >= FAILS_BEFORE_WIFI_RESET and \
            fail_count % FAILS_BEFORE_WIFI_RESET == 0:
        print("wifi: re-associating after %d failures" % fail_count)
        _wifi_reset()

    _maybe_reboot("no data")


def _retry_delay_s():
    """10, 20, 40, 80, ... capped - quick recovery from a one-off blip."""
    delay = RETRY_MIN_S << min(fail_count - 1, 5)
    return min(delay, RETRY_MAX_S, FETCH_INTERVAL_S)


def _on_net(_t):
    global status, next_fetch_ticks
    try:
        if not _pump_wifi():
            # No association yet, so a fetch cannot even be attempted. Once
            # data has been on screen, a long outage still ends in a reboot.
            _maybe_reboot("wifi down")
            render()
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, next_fetch_ticks) < 0:
            return
        if status != "ok":
            status = "fetch"
            render()
        try:
            fetch()
            wait_s = FETCH_INTERVAL_S
        except Exception as e:      # noqa: BLE001 - never let the timer die
            _note_failure(e)
            wait_s = _retry_delay_s()
        next_fetch_ticks = time.ticks_add(time.ticks_ms(), wait_s * 1000)
        gc.collect()
        render()
    except Exception as e:          # noqa: BLE001
        # An exception escaping an LVGL callback makes TaskHandler deinit
        # itself and the whole UI freezes - see firmware instructions §5.
        print("net timer error:", e)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

scr = lv.screen_active()
scr.set_style_bg_color(lv.color_hex(0x000000), 0)
scr.set_style_bg_opa(lv.OPA.COVER, 0)
scr.set_style_pad_all(0, 0)
scr.set_style_border_width(0, 0)
scr.add_flag(lv.obj.FLAG.CLICKABLE)


def _make_view():
    v = lv.obj(scr)
    v.set_size(_WIDTH, _HEIGHT)
    v.center()
    v.set_style_bg_opa(lv.OPA.TRANSP, 0)
    v.set_style_border_width(0, 0)
    v.set_style_pad_all(0, 0)
    v.remove_flag(lv.obj.FLAG.CLICKABLE)
    v.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return v


view_main = _make_view()
view_fallback = _make_view()


def _make_arc(diameter, color, parent=None):
    a = lv.arc(parent or view_main)
    a.set_size(diameter, diameter)
    a.center()
    a.set_rotation(270)              # 0 % at 12 o'clock
    a.set_bg_angles(0, 360)
    a.set_range(0, 100)
    a.remove_style(None, lv.PART.KNOB)
    a.remove_flag(lv.obj.FLAG.CLICKABLE)
    a.set_style_arc_width(12, lv.PART.MAIN)
    a.set_style_arc_width(12, lv.PART.INDICATOR)
    a.set_style_arc_color(TRACK, lv.PART.MAIN)
    a.set_style_arc_color(color, lv.PART.INDICATOR)
    a.set_style_arc_rounded(True, lv.PART.INDICATOR)
    return a


arc_5h = _make_arc(236, BLUE)
arc_wk = _make_arc(236 - 2 * 12 - 12, PURPLE)

lbl_title = lv.label(view_main)
lbl_title.set_style_text_font(FONT_SMALL, 0)

lbl_pct = lv.label(view_main)
lbl_pct.set_style_text_font(FONT_BIG, 0)
lbl_pct.set_style_text_color(WHITE, 0)

lbl_reset = lv.label(view_main)
lbl_reset.set_style_text_font(FONT_MED, 0)
lbl_reset.set_style_text_color(GRAY, 0)

lbl_other = lv.label(view_main)
lbl_other.set_style_text_font(FONT_SMALL, 0)


# Fallback view: everything the board can honestly show before the first
# successful fetch - what it is waiting on, and where it is pointed.
fb_ring = _make_arc(236, TRACK, view_fallback)
fb_ring.set_value(0)
fb_ring.set_style_arc_opa(lv.OPA.TRANSP, lv.PART.INDICATOR)

lbl_fb_title = lv.label(view_fallback)
lbl_fb_title.set_style_text_font(FONT_SMALL, 0)
lbl_fb_title.set_style_text_color(GRAY, 0)
lbl_fb_title.set_text("CLAUDE")

lbl_fb_msg = lv.label(view_fallback)
lbl_fb_msg.set_style_text_font(FONT_MED, 0)
lbl_fb_msg.set_style_text_color(WHITE, 0)

lbl_fb_host = lv.label(view_fallback)
lbl_fb_host.set_style_text_font(FONT_SMALL, 0)
lbl_fb_host.set_style_text_color(GRAY, 0)

_FB_MSG = {
    "wifi": "connecting Wi-Fi",
    "fetch": "reaching daemon",
    "err": "no daemon",
}


# Heartbeat: a dot for the link and the countdown to the next request. Both
# live on the screen rather than in a view, so they are the one thing on
# display in every state - the countdown keeps ticking even while nothing
# else can be shown, which is how a working board is told from a hung one.
lbl_next = lv.label(scr)
lbl_next.set_style_text_font(FONT_SMALL, 0)
lbl_next.set_style_text_color(GRAY, 0)

dot = lv.obj(scr)
dot.set_size(10, 10)
dot.set_style_radius(5, 0)
dot.set_style_border_width(0, 0)
dot.set_style_pad_all(0, 0)
dot.set_style_bg_opa(lv.OPA.COVER, 0)
dot.remove_flag(lv.obj.FLAG.CLICKABLE)
dot.remove_flag(lv.obj.FLAG.SCROLLABLE)


def _render_fallback():
    msg = _FB_MSG.get(status, "waiting for data")
    if status != "err":
        msg += "." * (_dots % 4)          # a heartbeat while it is trying
    lbl_fb_msg.set_text(msg)
    lbl_fb_host.set_text(TARGETS[_target_i][0])

    lbl_fb_title.align(lv.ALIGN.CENTER, 0, -40)
    lbl_fb_msg.align(lv.ALIGN.CENTER, 0, -8)
    lbl_fb_host.align(lv.ALIGN.CENTER, 0, 24)


def fmt_countdown(seconds):
    if seconds <= 0:
        return "NOW"
    minutes = int(seconds) // 60
    hours = minutes // 60
    if hours >= 24:
        return "{}d {}h".format(hours // 24, hours % 24)
    if hours > 0:
        return "{}h {}m".format(hours, minutes % 60)
    return "{}m".format(minutes)


def _remaining(base_s):
    """Countdown carried forward from the last fetch, or None if unknown."""
    if data_ticks is None or base_s < 0:
        return None
    elapsed = time.ticks_diff(time.ticks_ms(), data_ticks) // 1000
    return base_s - elapsed


def _fmt_reset(base_s):
    left = _remaining(base_s)
    return "" if left is None else fmt_countdown(left)


def _dot_color():
    """Green fetching happily, amber serving old figures, red disconnected."""
    if status == "err" or last_ok_ticks is None or not _wifi_ok():
        return RED
    # The daemon's own reading can go stale while it still answers, so its
    # age counts here too - an amber dot means "nobody is lying, but these
    # numbers are old".
    age = data_age_s + time.ticks_diff(time.ticks_ms(), last_ok_ticks) // 1000
    return AMBER if age > STALE_AFTER_S else GREEN


def _render_heartbeat():
    left = time.ticks_diff(next_fetch_ticks, time.ticks_ms()) // 1000
    if left < 0:
        left = 0
    lbl_next.set_text("{}:{:02d}".format(left // 60, left % 60))
    dot.set_style_bg_color(_dot_color(), 0)

    lbl_next.align(lv.ALIGN.CENTER, 8, 62)
    dot.align_to(lbl_next, lv.ALIGN.OUT_LEFT_MID, -7, 0)


def render():
    _render_heartbeat()

    if not have_data:
        view_main.add_flag(lv.obj.FLAG.HIDDEN)
        view_fallback.remove_flag(lv.obj.FLAG.HIDDEN)
        _render_fallback()
        return

    view_fallback.add_flag(lv.obj.FLAG.HIDDEN)
    view_main.remove_flag(lv.obj.FLAG.HIDDEN)

    arc_5h.set_value(five_hour_usage)
    arc_wk.set_value(weekly_usage)

    if show_five_hour:
        lbl_title.set_text("5 HOUR")
        lbl_title.set_style_text_color(BLUE, 0)
        lbl_pct.set_text("{}%".format(five_hour_usage))
        lbl_reset.set_text(_fmt_reset(five_hour_reset_s))
        lbl_other.set_text("week  {}%".format(weekly_usage))
        lbl_other.set_style_text_color(PURPLE, 0)
    else:
        lbl_title.set_text("WEEKLY")
        lbl_title.set_style_text_color(PURPLE, 0)
        lbl_pct.set_text("{}%".format(weekly_usage))
        lbl_reset.set_text(_fmt_reset(weekly_reset_s))
        lbl_other.set_text("5h  {}%".format(five_hour_usage))
        lbl_other.set_style_text_color(BLUE, 0)

    lbl_title.align(lv.ALIGN.CENTER, 0, -48)
    lbl_pct.align(lv.ALIGN.CENTER, 0, -16)
    lbl_reset.align(lv.ALIGN.CENTER, 0, 16)
    lbl_other.align(lv.ALIGN.CENTER, 0, 37)


def _on_click(_e):
    global show_five_hour
    try:
        show_five_hour = not show_five_hour
        render()
    except Exception as e:          # noqa: BLE001 - see §5, as in _on_net
        print("click handler error:", e)


def _on_tick(_t):
    global _dots
    try:
        _dots += 1
        render()
    except Exception as e:          # noqa: BLE001
        # render() allocates, so a fragmented heap could raise here. Left
        # unguarded that single exception would take TaskHandler - and with
        # it every timer - down for good.
        print("tick error:", e)
    if wdt is not None:
        # Fed only from inside the UI thread, so a frozen UI is a hung
        # watchdog and the board comes back by itself.
        wdt.feed()


wdt = None      # armed at the very end; _on_tick feeds it

# Module-level references: a callback that gets garbage collected stops
# firing silently (firmware instructions §5).
scr.add_event_cb(_on_click, lv.EVENT.CLICKED, None)
tick_timer = lv.timer_create(_on_tick, 1000, None)
net_timer = lv.timer_create(_on_net, 500, None)

render()


# ---------------------------------------------------------------------------
# Run - TaskHandler pumps lv.timer_handler() in the background
#
# The watchdog is armed last and cannot be switched off again, so everything
# that might block for a long time during start-up is already done. From
# here the board reboots unless _on_tick keeps running.
# ---------------------------------------------------------------------------

th = task_handler.TaskHandler()

if WATCHDOG:
    try:
        wdt = machine.WDT(timeout=WATCHDOG_S * 1000)
        print("watchdog armed: %ds" % WATCHDOG_S)
    except Exception as e:      # noqa: BLE001 - a port without WDT is fine
        print("no watchdog:", e)
