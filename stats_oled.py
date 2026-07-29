#!/usr/bin/env python3
"""Service that shows Raspberry Pi 5 system stats on an OLED.

Draws a status screen: a header with the device name (left) and uptime (right), then
six rows — CPU (load / temperature), RAM (used / total), SSD (free / total /
temperature), the network uplink (Wi-Fi SSID with a signal-bars icon, or ``LAN`` on a
wired link), IP address and fan (current rpm).
Each data row justifies a
regular-font label to the left edge and a bold-font value (same size) to the right edge;
the screen is assembled one line at a time through :class:`ScreenWriter` (rather than the
library's ready-made ``show_status``), which keeps per-line font control in the caller's
hands.

All low-level screen handling lives in the :mod:`oleddisplay` package; what remains here
is metric collection, the row layout, the refresh loop, and a night-time dimming window
that eases panel wear.
"""

__version__ = "1.0.0"

import argparse
import signal
import socket
import subprocess
import time
from collections import namedtuple
from pathlib import Path

import psutil

from oleddisplay import (
    DEFAULT_ADDRESS,
    DEFAULT_BUS,
    DEFAULT_ROTATE,
    OledDisplay,
    format_bytes,
    format_duration,
    format_percent,
)

HWMON_ROOT = Path("/sys/class/hwmon")
MISSING = "--"
WHITE = "white"

# --- Layout (in pixels) ------------------------------------------------------
# The header shows the device name (left) and uptime (right); the six data rows below
# justify a regular label to the left edge and a bold value to the right edge.
# Temperatures are shown as "47°" (degree sign only) to stay compact.
TITLE_SIZE = 12    # header font (device name + uptime)
BODY_SIZE = 10     # both label (regular) and value (bold) of the data rows
MARGIN = 3         # left/right screen margin
LABEL_GAP = 4      # minimum gap between a label and its right-aligned value
TITLE_TOP = 3      # y of the header row
ROWS_TOP = 24      # y of the first data row (small gap below the header)
ROW_STEP = 17      # vertical step between data rows (six rows fill the panel)

# --- Wi-Fi signal icon (ascending bars) --------------------------------------
# A tiny signal meter drawn at the right end of the NET row when it shows Wi-Fi: WIFI_BARS
# bars of growing height; the ones covered by the current signal are solid, the rest
# hollow outlines.
WIFI_BARS = 4          # number of bars
WIFI_BAR_WIDTH = 3     # px width of each bar (>=3 so a hollow bar shows an interior gap)
WIFI_BAR_GAP = 1       # px gap between bars
WIFI_BAR_MIN_H = 3     # height of the shortest (leftmost) bar
WIFI_BAR_STEP = 2      # each bar is this many px taller than the one before it
WIFI_ICON_GAP = 3      # gap between the icon and the SSID name to its left

Metrics = namedtuple("Metrics",
                     "hostname ip net_kind ssid wifi_signal cpu ram ssd fan uptime")
Sensors = namedtuple("Sensors", "cpu_temp ssd_temp fan")
NetStatus = namedtuple("NetStatus", "kind ssid signal")   # kind: "wifi" | "wired" | None


# --- Screen drawing ----------------------------------------------------------

class ScreenWriter:
    """Draw a status screen one line at a time, top to bottom.

    Wraps the PIL ``ImageDraw`` handed out by :meth:`OledDisplay.render` and keeps a
    y-cursor, so a caller writes the screen as a flat sequence of :meth:`row` calls
    instead of computing pixel coordinates. Every call chooses its own font(s), so the
    header, labels and values can each look different.
    """

    def __init__(self, display, draw, *, margin, top):
        self._display = display
        self._draw = draw
        self._margin = margin
        self._y = top

    def row(self, label, value, *, label_font, value_font, advance):
        """Draw a left-aligned ``label`` and a right-aligned ``value`` on one row.

        The label sits at the left margin; the value is justified to the right margin.
        If the value would collide with the label it is truncated with an ellipsis.

        :param label_font: font for the label (regular).
        :param value_font: font for the value (bold, same size as the label).
        :param advance: pixels to move the cursor down after the row.
        """
        self._draw.text((self._margin, self._y), label, font=label_font, fill=WHITE)
        label_right = self._margin + label_font.getlength(label)
        right_edge = self._display.width - self._margin
        cell_width = right_edge - (label_right + LABEL_GAP)
        fitted = _fit_width(value, value_font, cell_width)
        x = right_edge - value_font.getlength(fitted)
        self._draw.text((int(x), self._y), fitted, font=value_font, fill=WHITE)
        self._y += advance

    def wifi_row(self, label, name, signal, *, label_font, value_font, advance):
        """Draw the Wi-Fi NET row: ``label`` left, the network ``name`` right-aligned, and
        a Wi-Fi signal-bars icon pinned to the far right.

        :param name: the network name (already ``"--"`` when disconnected).
        :param signal: signal strength 0..100, or ``None`` (icon drawn empty).
        """
        self._draw.text((self._margin, self._y), label, font=label_font, fill=WHITE)
        right_edge = self._display.width - self._margin
        icon_left = _draw_wifi_bars(self._draw, right=right_edge,
                                    baseline=self._y + BODY_SIZE, signal=signal)
        name_right = icon_left - WIFI_ICON_GAP
        label_right = self._margin + label_font.getlength(label)
        cell_width = name_right - (label_right + LABEL_GAP)
        fitted = _fit_width(name, value_font, cell_width)
        x = name_right - value_font.getlength(fitted)
        self._draw.text((int(x), self._y), fitted, font=value_font, fill=WHITE)
        self._y += advance


def draw_dashboard(display, draw, metrics):
    """Draw the header (name left, uptime right) and the six data rows onto ``draw``."""
    writer = ScreenWriter(display, draw, margin=MARGIN, top=TITLE_TOP)
    # Header: the hostname on the left, the uptime on the right, both in the title font.
    header_font = display.font(TITLE_SIZE, bold=True)
    writer.row(metrics.hostname, metrics.uptime, label_font=header_font,
               value_font=header_font, advance=ROWS_TOP - TITLE_TOP)

    label_font = display.font(BODY_SIZE)
    value_font = display.font(BODY_SIZE, bold=True)

    def data_row(label, value):
        writer.row(label, value, label_font=label_font, value_font=value_font,
                   advance=ROW_STEP)

    data_row("CPU:", metrics.cpu)
    data_row("RAM:", metrics.ram)
    data_row("SSD:", metrics.ssd)
    # NET row: Wi-Fi shows "<SSID> <signal-bars>", a wired uplink shows "LAN", and no
    # uplink shows the "--" placeholder.
    if metrics.net_kind == "wifi":
        writer.wifi_row("NET:", metrics.ssid, metrics.wifi_signal,
                        label_font=label_font, value_font=value_font, advance=ROW_STEP)
    elif metrics.net_kind == "wired":
        data_row("NET:", "LAN")
    else:
        data_row("NET:", MISSING)
    data_row("IP:", metrics.ip)
    data_row("Fan:", metrics.fan)


def show_dashboard(display, metrics):
    """Render one full frame of the status screen to the panel."""
    def paint(draw):
        draw_dashboard(display, draw, metrics)

    display.render(paint)


def _fit_width(text, font, max_width):
    """Return ``text`` truncated with an ellipsis so it fits within ``max_width`` px."""
    if font.getlength(text) <= max_width:
        return text
    ellipsis = "…"
    truncated = text
    while truncated and font.getlength(truncated + ellipsis) > max_width:
        truncated = truncated[:-1]
    return truncated + ellipsis


def signal_to_bars(signal):
    """Map a Wi-Fi ``signal`` (0..100, or ``None``) to the number of filled bars.

    Splits the range into ``WIFI_BARS`` even bands; a connected-but-faint signal still
    lights one bar, and ``None``/0 lights none.
    """
    if not signal or signal <= 0:
        return 0
    filled = (signal + 24) // 25   # 1..25→1, 26..50→2, 51..75→3, 76..100→4
    return min(filled, WIFI_BARS)


def _draw_wifi_bars(draw, *, right, baseline, signal):
    """Draw the Wi-Fi icon with its right edge at ``right`` and bottom at ``baseline``.

    Bars ascend left→right; the first ``signal_to_bars(signal)`` are solid, the rest are
    hollow. Returns the icon's left x so the caller can right-align the name beside it.
    """
    filled = signal_to_bars(signal)
    icon_width = WIFI_BARS * WIFI_BAR_WIDTH + (WIFI_BARS - 1) * WIFI_BAR_GAP
    left = right - icon_width
    for i in range(WIFI_BARS):
        bar_height = WIFI_BAR_MIN_H + i * WIFI_BAR_STEP
        x0 = left + i * (WIFI_BAR_WIDTH + WIFI_BAR_GAP)
        x1 = x0 + WIFI_BAR_WIDTH - 1
        top = baseline - bar_height
        if i < filled:
            draw.rectangle((x0, top, x1, baseline), fill=WHITE)
        else:
            draw.rectangle((x0, top, x1, baseline), outline=WHITE)
    return left


# --- Metric collection -------------------------------------------------------

def find_hwmon_input(target_name, input_file):
    """Find ``input_file`` under the hwmon device named ``target_name`` (or ``None``).

    ``hwmonN`` numbering can change across reboots, so we look the device up by its name
    (``cpu_thermal``, ``nvme``, ``pwmfan``) rather than by a fixed path.
    """
    if not HWMON_ROOT.exists():
        return None
    for hwmon in sorted(HWMON_ROOT.glob("hwmon*")):
        name_file = hwmon / "name"
        if not name_file.exists():
            continue
        if name_file.read_text().strip() != target_name:
            continue
        candidate = hwmon / input_file
        if candidate.exists():
            return candidate
    return None


def read_temp_c(temp_path):
    """Read a temperature in °C from an hwmon file (the value is stored in milli-°C)."""
    if temp_path is None:
        return None
    try:
        raw = temp_path.read_text().strip()
    except OSError:
        return None
    milli = int(raw) if raw.lstrip("-").isdigit() else 0
    return milli / 1000.0


def read_fan_rpm(fan_path):
    """Read the fan speed in RPM from an hwmon ``fan*_input`` file (``None`` if absent)."""
    if fan_path is None:
        return None
    try:
        raw = fan_path.read_text().strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def read_primary_ip():
    """Return this host's primary IPv4 — the source address of the default route.

    Opens a UDP socket "connected" to a public address: no packet is actually sent, the
    kernel merely resolves which local address it would use, which is the Wi-Fi IP here.
    Returns ``None`` if there is no route (e.g. the network is down).
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def read_wifi():
    """Return ``(ssid, signal)`` for the joined Wi-Fi network, or ``(None, None)``.

    Reads both from NetworkManager in a single ``nmcli`` call (``iwgetid`` is not installed
    on this box); ``signal`` is a 0..100 strength. ``nmcli -t`` prints one
    ``active:ssid:signal`` line per known network and escapes a literal ``:`` inside a
    field as ``\\:``. ``signal`` is the trailing numeric field, so we split it off the
    right and unescape what remains as the SSID.
    """
    try:
        completed = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid,signal", "dev", "wifi"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    for line in completed.stdout.splitlines():
        if not line.startswith("yes:"):
            continue
        rest = line[len("yes:"):]
        ssid_raw, separator, signal_raw = rest.rpartition(":")
        if separator == "":
            ssid_raw, signal_raw = rest, ""
        ssid = ssid_raw.replace("\\:", ":") or None
        signal = int(signal_raw) if signal_raw.isdigit() else None
        return ssid, signal
    return None, None


def read_default_iface():
    """Return the interface carrying the default route, or ``None`` (network down).

    Asks the kernel which interface it would use to reach a public address; the
    ``dev <iface>`` field of ``ip route get`` is the current uplink — ``wlan0`` on Wi-Fi,
    ``eth0`` on a cable.
    """
    try:
        completed = subprocess.run(
            ["ip", "route", "get", "8.8.8.8"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = completed.stdout.split()
    if "dev" not in fields:
        return None
    dev_index = fields.index("dev")
    if dev_index + 1 >= len(fields):
        return None
    return fields[dev_index + 1]


def is_wireless(iface):
    """Whether ``iface`` is a Wi-Fi interface, per its sysfs markers."""
    base = Path("/sys/class/net") / iface
    return (base / "wireless").exists() or (base / "phy80211").exists()


def read_network():
    """Classify the active uplink as a :class:`NetStatus`.

    Looks at the interface carrying the default route: a wireless one is reported as
    ``"wifi"`` (with its SSID and signal), a wired one as ``"wired"``, and no route as a
    disconnected state (``kind`` is ``None``).
    """
    iface = read_default_iface()
    if iface is None:
        return NetStatus(kind=None, ssid=None, signal=None)
    if is_wireless(iface):
        ssid, signal = read_wifi()
        return NetStatus(kind="wifi", ssid=ssid, signal=signal)
    return NetStatus(kind="wired", ssid=None, signal=None)


def read_uptime_seconds():
    """Return the seconds elapsed since boot."""
    return time.time() - psutil.boot_time()


def or_missing(value):
    """Show real data or the ``--`` placeholder for a missing/empty value."""
    return value if value else MISSING


def format_temp(celsius):
    """A temperature in °C → a compact ``"47°"`` (degree sign only). ``None`` → ``"--"``."""
    if celsius is None:
        return MISSING
    return f"{celsius:.0f}°"


def format_fan(rpm):
    """Fan speed with its unit, e.g. ``"2400 rpm"`` — always shown, even ``"0 rpm"``.

    Only a missing sensor (``None``) collapses to ``"--"``.
    """
    if rpm is None:
        return MISSING
    return f"{rpm} rpm"


def format_ram(used_bytes, total_bytes):
    """RAM as ``"<used> / <total>"`` in GiB, e.g. ``"1.7G / 8G"`` (used to one decimal).

    "Used" is the standard ``total - available`` — memory that is not readily available
    for new allocations (matching the usual "% used" that htop/btop report).
    """
    used_gib = used_bytes / 1024 ** 3
    total_gib = total_bytes / 1024 ** 3
    return f"{used_gib:.1f}G / {total_gib:.0f}G"


def collect_metrics(hostname, sensors):
    """Collect the whole screen as a :class:`Metrics` snapshot."""
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu_load = format_percent(psutil.cpu_percent(interval=None))
    cpu_temp = format_temp(read_temp_c(sensors.cpu_temp))
    ssd_free = format_bytes(disk.free)
    ssd_total = format_bytes(disk.total)
    ssd_temp = format_temp(read_temp_c(sensors.ssd_temp))
    net = read_network()
    return Metrics(
        hostname=hostname,
        ip=or_missing(read_primary_ip()),
        net_kind=net.kind,
        ssid=or_missing(net.ssid),
        wifi_signal=net.signal,
        cpu=f"{cpu_load} / {cpu_temp}",
        ram=format_ram(vm.total - vm.available, vm.total),
        ssd=f"{ssd_free}/{ssd_total} {ssd_temp}",
        fan=format_fan(read_fan_rpm(sensors.fan)),
        uptime=format_duration(read_uptime_seconds()),
    )


# --- Night dimming -----------------------------------------------------------

def is_night(hour, *, start, end):
    """Whether ``hour`` (0..23) falls in the ``[start, end)`` night window.

    Handles a window that wraps past midnight (e.g. ``start=22, end=6``). An empty window
    (``start == end``) is never night.
    """
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def pick_contrast(hour, *, day, night, night_start, night_end):
    """Choose the brightness for ``hour``: ``night`` inside the window, else ``day``.

    ``night`` is ``None`` when night dimming is off. The result can be ``None`` (day value
    not set → leave the panel as-is).
    """
    if night is not None and is_night(hour, start=night_start, end=night_end):
        return night
    return day


# --- Refresh loop ------------------------------------------------------------

def _raise_keyboard_interrupt(signum, frame):
    """Turn SIGTERM (``systemctl stop`` / shutdown) into the same clean exit as Ctrl-C.

    Without this, SIGTERM kills the process without unwinding the ``with`` block, so the
    display is never blanked and the last frame stays burning in while the Pi is powered
    off but still on mains.
    """
    raise KeyboardInterrupt


def run(display, args, hostname, sensors):
    """Draw a single frame (``--once``) or loop the refresh until interrupted."""
    if args.once:
        # A short pause so the first cpu_percent reading is meaningful.
        time.sleep(0.7)
        show_dashboard(display, collect_metrics(hostname, sensors))
        return

    applied_contrast = args.contrast   # already applied by OledDisplay.open()
    try:
        while True:
            wanted = pick_contrast(
                time.localtime().tm_hour,
                day=args.contrast, night=args.night_contrast,
                night_start=args.night_start, night_end=args.night_end,
            )
            if wanted is not None and wanted != applied_contrast:
                display.set_contrast(wanted)
                applied_contrast = wanted

            show_dashboard(display, collect_metrics(hostname, sensors))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def parse_args():
    parser = argparse.ArgumentParser(description="OLED system stats for Raspberry Pi 5")
    parser.add_argument("--bus", type=int, default=DEFAULT_BUS,
                        help=f"I2C bus number (default {DEFAULT_BUS})")
    parser.add_argument("--address", type=lambda x: int(x, 0), default=DEFAULT_ADDRESS,
                        help="I2C display address (default 0x3c)")
    parser.add_argument("--rotate", type=int, choices=[0, 1, 2, 3], default=DEFAULT_ROTATE,
                        help=f"rotation 0/1/2/3 = 0/90/180/270° (default {DEFAULT_ROTATE})")
    parser.add_argument("--contrast", type=int, default=None,
                        help="panel brightness 0..255 (default: leave the panel default)")
    parser.add_argument("--night-contrast", type=int, default=None,
                        help="dim to this brightness (0..255) during the night window; "
                             "disabled when omitted (needs --contrast as the day value)")
    parser.add_argument("--night-start", type=int, default=0,
                        help="night window start hour 0..23 (default 0)")
    parser.add_argument("--night-end", type=int, default=6,
                        help="night window end hour 0..23, exclusive (default 6)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="refresh period in seconds (default 5)")
    parser.add_argument("--once", action="store_true",
                        help="draw a single frame and exit (the frame stays on screen)")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.night_contrast is not None and args.contrast is None:
        raise SystemExit("--night-contrast needs --contrast (the daytime brightness) to "
                         "restore to when the night window ends")

    # SIGTERM (systemctl stop / shutdown) should blank the panel like Ctrl-C, so the last
    # frame is not left burning in while the Pi is off but still on mains.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    hostname = socket.gethostname()
    sensors = Sensors(
        cpu_temp=find_hwmon_input("cpu_thermal", "temp1_input"),
        ssd_temp=find_hwmon_input("nvme", "temp1_input"),
        fan=find_hwmon_input("pwmfan", "fan1_input"),
    )

    # Prime psutil.cpu_percent, otherwise the first reading returns 0.
    psutil.cpu_percent(interval=None)

    # In --once mode keep the frame on screen; in the loop, clear it on exit
    # (Ctrl-C or SIGTERM).
    with OledDisplay.open(bus=args.bus, address=args.address, rotate=args.rotate,
                          contrast=args.contrast, clear_on_close=not args.once) as display:
        run(display, args, hostname, sensors)


if __name__ == "__main__":
    main()
