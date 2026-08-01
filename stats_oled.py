#!/usr/bin/env python3
"""Service that shows Raspberry Pi 5 system stats on an OLED.

Draws a status screen: a header with the device name (left) and uptime (right), then up
to six rows — CPU (load / temperature), RAM (used / total), the root disk (free / total /
temperature, labelled ``SSD`` or ``SD`` after the actual medium), the network uplink
(Wi-Fi SSID with a signal-bars icon, or ``LAN`` on a wired link), IP address and, when a
fan is present, its speed (current rpm — the row is hidden on fanless boards).
Each data row justifies a regular-font label to the left edge and a bold-font value (same
size) to the right edge; the screen is built from the display's building blocks — one
:meth:`~oleddisplay.OledDisplay.show_columns` call per row — with the Wi-Fi row keeping an
empty right column for a signal-bars icon that :meth:`~oleddisplay.OledDisplay.show_custom`
paints on top.

All low-level screen handling lives in the :mod:`oleddisplay` package; what remains here
is metric collection, the row layout, the refresh loop, and a night-time dimming window
that eases panel wear. The loop also alternates the dashboard with a weather page (current
temperature, high/low and conditions from :mod:`weather`) and an indoor sensor page (room
temperature, humidity and pressure from :mod:`local_meteo`'s AHT20+BMP280), picking the due
page with :func:`oleddisplay.due_page_index`.
"""

__version__ = "1.11.0"

import argparse
import math
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
    due_page_index,
    format_bytes,
    format_duration,
    format_percent,
    format_temperature,
)

import config
import local_meteo
import weather
import webconfig

HWMON_ROOT = Path("/sys/class/hwmon")
MISSING = "--"
WHITE = "white"
BLACK = "black"

# --- Dashboard layout --------------------------------------------------------
# The screen is stacked from the display's building blocks in order: the header row
# (device name left, uptime right) and each data row are one show_columns() call — a
# regular-font label pinned left, a bold value pinned right — and the Wi-Fi NET row adds
# its signal-bars icon on top with show_custom(). Blocks stack top-down in the order added;
# nothing is stretched to fill the panel. Temperatures are shown as "47°" to stay compact.
TITLE_SIZE = 13    # header font (device name + uptime)
BODY_SIZE = 11     # label (regular) + value (bold) of the data rows
MARGIN = 1         # left/right margin for the weather page's custom-drawn text

# A data row is two columns: a label pinned left and a value pinned right (percent of the
# panel width, passed per row; the value column is wide so long values keep their right edge).
ROW_ALIGNS = ("left", "right")
ROW_STYLES = ("regular", "bold")   # label regular, value bold — same family and size

# The Wi-Fi NET row keeps a third, empty column on the right so the SSID never runs under
# the signal-bars icon; WIFI_ICON_BOX below places that icon over it.
NET_ALIGNS = ("left", "right", "right")
NET_STYLES = ("regular", "bold", "regular")

# --- Wi-Fi signal icon (ascending bars) --------------------------------------
# A tiny signal meter painted with show_custom() on the right of the NET row: WIFI_BARS
# bars of growing height; the ones covered by the current signal are solid, the rest
# hollow outlines. Its right edge is a fixed panel-relative x (the reserved NET column
# never moves sideways), but its *vertical* position is read from the display cursor at
# draw time (see show_dashboard) so the icon rides the NET row instead of a hardcoded y
# that drifts whenever the rows above it change.
WIFI_BARS = 4          # number of bars
WIFI_BAR_WIDTH = 3     # px width of each bar (>=3 so a hollow bar shows an interior gap)
WIFI_BAR_GAP = 1       # px gap between bars
WIFI_BAR_MIN_H = 3     # height of the shortest (leftmost) bar
WIFI_BAR_STEP = 2      # each bar is this many px taller than the one before it
WIFI_ICON_RIGHT = 124  # x of the icon's right edge (panel-relative; the reserved NET column)
WIFI_ICON_LIFT = 3     # px the icon's bottom sits above the NET row's bottom (≈ font descent),
                       # so the bars line up with the SSID text baseline

Metrics = namedtuple(
    "Metrics",
    "hostname ip net_kind ssid wifi_signal cpu ram disk_label ssd fan uptime",
)
Sensors = namedtuple("Sensors", "cpu_temp ssd_temp fan")
NetStatus = namedtuple("NetStatus", "kind ssid signal")   # kind: "wifi" | "wired" | None
Services = namedtuple("Services", "weather meteo")   # the optional background page services


# --- Screen drawing ----------------------------------------------------------

def show_dashboard(display, metrics, layout_rows):
    """Build the status screen from the display's building blocks and show it as one frame.

    The header (device name left, uptime right) and each data row are a
    :meth:`~oleddisplay.OledDisplay.show_columns` call — a regular-font label pinned left, a
    bold value pinned right. Which data rows appear, and in what order, comes from
    ``layout_rows`` (the web-editable :class:`config.LayoutRow` list): disabled rows are
    skipped, and a one-pixel gap separates the rows that remain. The Wi-Fi NET row keeps an
    empty right column for its signal-bars icon, which
    :meth:`~oleddisplay.OledDisplay.show_custom` paints on top; a wired or missing uplink is
    a plain row. Blocks stack top-down in the order added.
    """
    # Header: hostname on the left, uptime on the right, both in the bold title font.
    display.set_font("sans", TITLE_SIZE)
    display.show_columns([[metrics.hostname, metrics.uptime]], [60, 50], ROW_ALIGNS, ("bold", "regular"))

    # Data rows: a regular label pinned left, a bold value pinned right.
    display.set_font("sans", BODY_SIZE)

    display.show_line("-------------------------------")

    # Draw the enabled rows in the configured order, with a 1 px gap between them. The fan
    # row is dropped on fanless boards regardless of the config (see _row_available).
    rows_to_draw = [row.key for row in layout_rows
                    if row.enabled and _row_available(row.key, metrics)]
    for index, key in enumerate(rows_to_draw):
        if index > 0:
            display.show_empty_line(1)
        _draw_row(display, key, metrics)

    display.show()

    display.set_font()   # don't leak the dashboard font into the other screens


def _row_available(key, metrics):
    """Whether a row has data to show right now — the fan row hides on fanless boards."""
    if key == "fan":
        return metrics.fan is not None
    return True


def _draw_kv_row(display, label, value, widths):
    """Draw a plain label/value row: a regular label pinned left, a bold value pinned right."""
    display.show_columns([[label, value]], widths, ROW_ALIGNS, ROW_STYLES)


def _draw_row(display, key, metrics):
    """Draw one system-info row by its config key (see :data:`config.ROW_DEFS`).

    Most rows are a plain label/value pair; NET is special — it reserves a right column for
    the Wi-Fi signal icon and paints it on top. An unknown key (a config newer than this
    code) is skipped quietly.
    """
    if key == "cpu":
        _draw_kv_row(display, "CPU:", metrics.cpu, [23, 77])
    elif key == "ram":
        _draw_kv_row(display, "RAM:", metrics.ram, [23, 77])
    elif key == "disk":
        # Root disk: the label is "SSD" or "SD" depending on the medium backing "/".
        _draw_kv_row(display, f"{metrics.disk_label}:", metrics.ssd, [23, 77])
    elif key == "net":
        _draw_net_row(display, metrics)
    elif key == "ip":
        _draw_kv_row(display, "IP:", metrics.ip, [12, 88])
    elif key == "fan":
        _draw_kv_row(display, "FAN:", metrics.fan, [23, 77])
    else:
        pass


def _draw_net_row(display, metrics):
    """Draw the NET row: Wi-Fi (SSID + signal icon), a wired "LAN", or the "--" placeholder.

    Wi-Fi shows the SSID with a signal-bars icon painted over the reserved right column; a
    wired uplink shows "LAN" and no uplink shows "--".
    """
    if metrics.net_kind == "wifi":
        display.show_columns([["NET:", metrics.ssid, ""]], [23, 57, 20], NET_ALIGNS, NET_STYLES)

        # The cursor now sits at the NET row's bottom edge, so pinning the icon to it (minus a
        # small lift onto the text baseline) keeps the bars on the row no matter how the rows
        # above change — no more re-tuning an absolute y.
        net_row_bottom = display.cursor_y
        display.show_custom(
            lambda draw: _draw_wifi_bars(draw, right=WIFI_ICON_RIGHT,
                                                 baseline=net_row_bottom - WIFI_ICON_LIFT,
                                                 signal=metrics.wifi_signal))
    elif metrics.net_kind == "wired":
        _draw_kv_row(display, "NET:", "LAN", [23, 77])
    else:
        _draw_kv_row(display, "NET:", MISSING, [23, 77])


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
    hollow. Returns the icon's left x (unused now the SSID column reserves its own space).
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


# --- Weather page ------------------------------------------------------------
# A second screen the loop alternates with the dashboard. The whole page is constructed
# from the oleddisplay building blocks: show_custom paints the header — the big current
# temperature with a condition icon (day/night aware) top-right — and set_font /
# show_line / show_empty_line stack the text under it; display.show() then puts the
# finished screen on the panel as a single frame. The icon is drawn from primitives
# (like the Wi-Fi bars) in an outline style that keeps lit pixels — and burn-in — low.
WX_TEMP_SIZE = 33    # big current-temperature number
WX_UNIT_SIZE = 15    # the "C" unit beside the big number
WX_HILO_SIZE = 13    # the day's high / low
WX_ROW_SIZE = 13     # the time / date / city rows
WX_TEMP_TOP = 4      # y of the big temperature
WX_ICON_BOX = (82, 6, 124, 46)   # (x0, y0, x1, y1) box for the condition icon
WX_BODY_TOP = 41     # y where the block-built body starts, under the temperature / icon
WX_HILO_GAP = 2      # extra blank px after the high/low line (17 px line + 2 = 19 px step)
WX_ROW_GAP = 1       # extra blank px after the time / date rows (17 px line + 1 = 18 px step)

TIME_SIZE = 17
CITY_GAP = 12


def _round_temp(value):
    """A temperature rounded to a whole number for display; ``None`` → ``"--"``."""
    if value is None:
        return MISSING
    return f"{value:.0f}"


def _draw_weather_header(display, draw, data):
    """The custom top of the weather page: the big temperature and the condition icon."""
    # Big current temperature, e.g. "12°" with a smaller "C" tucked to its right.
    temp_font = display.font(WX_TEMP_SIZE, bold=True)
    unit_font = display.font(WX_UNIT_SIZE, bold=True)
    temp_text = f"{_round_temp(data.temp)}°"
    draw.text((MARGIN, WX_TEMP_TOP), temp_text, font=temp_font, fill=WHITE)
    unit_x = MARGIN + temp_font.getlength(temp_text) + 1
    draw.text((unit_x, WX_TEMP_TOP + WX_TEMP_SIZE - WX_UNIT_SIZE - 3),
              "C", font=unit_font, fill=WHITE)

    # Condition icon, top-right, day/night aware.
    _draw_weather_icon(draw, WX_ICON_BOX, data.category, data.is_day)


def show_weather(display, data):
    """Show the weather page: the custom header on top, a block-built body under it.

    ``data`` is the latest :class:`weather.WeatherData` (``None`` before the first fetch).
    The screen is constructed from the display's building blocks — the custom-drawn
    header, then the high/low, time, date and city as plain text stacked top-down — and
    display.show() puts it on the panel as one frame.
    """
    if data is None:
        def paint(draw):
            _draw_weather_loading(display, draw)
        display.render(paint)
        return

    now = time.localtime()

    def paint_header(draw):
        _draw_weather_header(display, draw, data)

    display.show_custom(paint_header)
    display.show_empty_line(WX_BODY_TOP)

    display.set_font("sans", WX_HILO_SIZE)
    display.show_line(f"{_round_temp(data.high)}° / {_round_temp(data.low)}°")

    display.show_empty_line(WX_HILO_GAP)

    display.set_font("sans", TIME_SIZE, "bold")
    display.show_line(time.strftime("%H:%M", now))

    display.show_empty_line(WX_ROW_GAP)

    display.set_font("sans", WX_ROW_SIZE)
    display.show_line(time.strftime("%a, %d %b", now))

    display.show_empty_line(CITY_GAP)

    display.set_font("sans", WX_ROW_SIZE, "bold")
    display.show_line(data.city, "right")

    display.show()

    display.set_font()   # don't leak the weather font into the other screens


def _draw_weather_loading(display, draw):
    """Placeholder shown until the first weather reading arrives."""
    font = display.font(14)
    draw.text((MARGIN, display.height // 2 - 8), "Weather…", font=font, fill=WHITE)


def _draw_weather_icon(draw, box, category, is_day):
    """Draw the condition icon inside ``box`` (x0, y0, x1, y1), day/night aware."""
    if category == weather.CLEAR:
        if is_day:
            _draw_sun(draw, box)
        else:
            _draw_moon(draw, box)
    elif category == weather.PARTLY:
        _draw_partly(draw, box, is_day)
    elif category == weather.FOG:
        _draw_fog(draw, box)
    elif category == weather.RAIN:
        _draw_cloud_with(draw, box, "rain")
    elif category == weather.SNOW:
        _draw_cloud_with(draw, box, "snow")
    elif category == weather.THUNDER:
        _draw_cloud_with(draw, box, "bolt")
    else:   # CLOUDY and any unknown category
        _draw_cloud(draw, box)


def _draw_sun(draw, box):
    """A sun: an outline disc with eight rays, centred in ``box``."""
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    r = min(x1 - x0, y1 - y0) * 0.25
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=WHITE, width=2)
    for degrees in range(0, 360, 45):
        angle = math.radians(degrees)
        dx, dy = math.cos(angle), math.sin(angle)
        draw.line((cx + dx * (r + 3), cy + dy * (r + 3),
                   cx + dx * (r + 7), cy + dy * (r + 7)), fill=WHITE, width=2)


def _draw_moon(draw, box):
    """A crescent moon: a white disc with an offset black disc carved out of it."""
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    r = min(x1 - x0, y1 - y0) * 0.38
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WHITE)
    dx, dy = r * 0.55, -r * 0.25
    draw.ellipse((cx - r + dx, cy - r + dy, cx + r + dx, cy + r + dy), fill=BLACK)


def _cloud_shapes(box):
    """Return the primitives that make up a cloud in ``box``: a base slab + three lobes."""
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    base_top = y1 - h * 0.45
    slab = (x0, base_top, x1, y1)
    radius = h * 0.22
    lobes = [
        (x0 + w * 0.30, base_top - h * 0.02, h * 0.26),
        (x0 + w * 0.52, y0 + h * 0.24, h * 0.30),
        (x0 + w * 0.74, base_top - h * 0.05, h * 0.24),
    ]
    return slab, radius, lobes


def _fill_cloud(draw, box, color, *, erode=0):
    """Fill a cloud silhouette in ``color``; ``erode`` shrinks each primitive inward."""
    (sx0, stop, sx1, sy1), radius, lobes = _cloud_shapes(box)
    draw.rounded_rectangle((sx0 + erode, stop + erode, sx1 - erode, sy1 - erode),
                           radius=max(1, radius - erode), fill=color)
    for cx, cy, r in lobes:
        rr = r - erode
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=color)


def _draw_cloud(draw, box):
    """An outline cloud: a filled silhouette with an eroded black one carving its inside."""
    _fill_cloud(draw, box, WHITE)
    _fill_cloud(draw, box, BLACK, erode=2)


def _draw_partly(draw, box, is_day):
    """Sun (or moon) peeking from the top-left with a cloud overlapping its lower-right."""
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    body = (x0, y0, x0 + w * 0.60, y0 + h * 0.60)
    if is_day:
        _draw_sun(draw, body)
    else:
        _draw_moon(draw, body)
    _draw_cloud(draw, (x0 + w * 0.22, y0 + h * 0.32, x1, y1))


def _draw_cloud_with(draw, box, extra):
    """A cloud in the upper part of ``box`` with rain / snow / a bolt beneath it."""
    x0, y0, x1, y1 = box
    h = y1 - y0
    _draw_cloud(draw, (x0, y0, x1, y0 + h * 0.68))
    below_top = y0 + h * 0.72
    if extra == "rain":
        _draw_rain(draw, x0, x1, below_top, y1)
    elif extra == "snow":
        _draw_snow(draw, x0, x1, below_top, y1)
    else:   # "bolt"
        _draw_bolt(draw, (x0 + x1) / 2, below_top, y1)


def _draw_rain(draw, x0, x1, top, bottom):
    """Three short diagonal rain streaks between ``top`` and ``bottom``."""
    w = x1 - x0
    for i in range(3):
        x = x0 + w * (0.28 + i * 0.22)
        draw.line((x, top, x - 3, bottom), fill=WHITE, width=2)


def _draw_snow(draw, x0, x1, top, bottom):
    """Three small snowflakes between ``top`` and ``bottom``."""
    w = x1 - x0
    mid = (top + bottom) / 2
    for i in range(3):
        x = x0 + w * (0.28 + i * 0.22)
        for degrees in (0, 60, 120):
            angle = math.radians(degrees)
            dx, dy = math.cos(angle) * 3, math.sin(angle) * 3
            draw.line((x - dx, mid - dy, x + dx, mid + dy), fill=WHITE, width=1)


def _draw_bolt(draw, cx, top, bottom):
    """A lightning bolt centred at ``cx`` between ``top`` and ``bottom``."""
    h = bottom - top
    draw.polygon([
        (cx + 3, top),
        (cx - 4, top + h * 0.55),
        (cx, top + h * 0.55),
        (cx - 2, bottom),
        (cx + 5, top + h * 0.40),
        (cx + 1, top + h * 0.40),
    ], fill=WHITE)


def _draw_fog(draw, box):
    """A small cloud above three horizontal fog lines."""
    x0, y0, x1, y1 = box
    h = y1 - y0
    _draw_cloud(draw, (x0, y0, x1, y0 + h * 0.58))
    for i in range(3):
        y = y0 + h * (0.72 + i * 0.13)
        draw.line((x0 + 4, y, x1 - 4, y), fill=WHITE, width=2)


# --- Indoor meteo page -------------------------------------------------------
# A third screen the loop alternates with the dashboard and weather: the local AHT20+BMP280
# readings (room temperature, humidity, pressure) from :mod:`local_meteo`. Built like the
# weather page — a custom header with the big room temperature and a small house icon that
# marks this as the indoor page (parallel to the weather page's condition icon) — then humidity
# and pressure as plain label/value rows.
MT_TEMP_SIZE = 26    # big room-temperature number (fits "26.8°C" left of the icon)
MT_UNIT_SIZE = 13    # the "C" unit beside the big number
MT_TEMP_TOP = 8      # y of the big temperature
MT_ICON_BOX = (97, 7, 124, 34)   # (x0, y0, x1, y1) box for the house icon
MT_BODY_TOP = 44     # y where the humidity/pressure rows start, under the header
MT_ROW_SIZE = 12     # font of the humidity / pressure rows (12px fits bold "1009 hPa" on 126px)
MT_ROW_GAP = 6       # blank px between the two rows
MT_ROW_WIDTHS = [46, 54]   # label / value split; the value is bold (wider) so it gets more


def _format_temp1(value):
    """A temperature to one decimal for display, e.g. ``"26.8"``; ``None`` → ``"--"``."""
    if value is None:
        return MISSING
    return f"{value:.1f}"


def _format_humidity(value):
    """Relative humidity as a whole percent, e.g. ``"54%"``; ``None`` → ``"--"``."""
    if value is None:
        return MISSING
    return f"{value:.0f}%"


def _format_pressure(value):
    """Barometric pressure as whole hPa, e.g. ``"1009 hPa"``; ``None`` → ``"--"``."""
    if value is None:
        return MISSING
    return f"{value:.0f} hPa"


def _draw_house(draw, box):
    """A small house icon (roof + walls + door) marking the indoor sensor page."""
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    eaves = y0 + h * 0.42
    # Roof: a triangle spanning the full width.
    draw.polygon([(x0, eaves), ((x0 + x1) / 2, y0), (x1, eaves)], outline=WHITE, width=2)
    # Walls: an outlined box under the eaves, slightly inset from the roof edges.
    inset = w * 0.12
    draw.rectangle((x0 + inset, eaves, x1 - inset, y1), outline=WHITE, width=2)
    # Door: a small filled slab centred at the bottom.
    door_w = w * 0.22
    door_cx = (x0 + x1) / 2
    draw.rectangle((door_cx - door_w / 2, y1 - h * 0.34, door_cx + door_w / 2, y1), fill=WHITE)


def _draw_meteo_header(display, draw, reading):
    """The custom top of the meteo page: the big room temperature and the house icon."""
    temp_font = display.font(MT_TEMP_SIZE, bold=True)
    unit_font = display.font(MT_UNIT_SIZE, bold=True)
    temp_text = f"{_format_temp1(reading.temp_c)}°"
    draw.text((MARGIN, MT_TEMP_TOP), temp_text, font=temp_font, fill=WHITE)
    unit_x = MARGIN + temp_font.getlength(temp_text) + 1
    draw.text((unit_x, MT_TEMP_TOP + MT_TEMP_SIZE - MT_UNIT_SIZE - 3),
              "C", font=unit_font, fill=WHITE)
    _draw_house(draw, MT_ICON_BOX)


def show_meteo(display, reading):
    """Show the indoor meteo page: big temperature + house icon, then humidity and pressure.

    ``reading`` is the latest :class:`local_meteo.MeteoReading` (``None`` before the first
    sensor read). Any single field may be ``None`` if that sensor failed; it renders as ``--``.
    The screen is built from the display's building blocks — the custom header, then the two
    rows stacked under it — and ``display.show()`` puts it on the panel as one frame.
    """
    if reading is None:
        def paint(draw):
            font = display.font(14)
            draw.text((MARGIN, display.height // 2 - 8), "Sensor…", font=font, fill=WHITE)
        display.render(paint)
        return

    def paint_header(draw):
        _draw_meteo_header(display, draw, reading)

    display.show_custom(paint_header)
    display.show_empty_line(MT_BODY_TOP)

    display.set_font("sans", MT_ROW_SIZE)
    display.show_columns([["Humidity", _format_humidity(reading.humidity)]],
                         MT_ROW_WIDTHS, ROW_ALIGNS, ROW_STYLES)
    display.show_empty_line(MT_ROW_GAP)
    display.show_columns([["Pressure", _format_pressure(reading.pressure_hpa)]],
                         MT_ROW_WIDTHS, ROW_ALIGNS, ROW_STYLES)

    display.show()

    display.set_font()   # don't leak the meteo font into the other screens


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


def read_disk_label():
    """Return the root storage row's label: ``"SD"`` for an SD/eMMC card, else ``"SSD"``.

    Names the row after the medium backing ``/``: an SD card sits on an ``mmcblk*`` block
    device, whereas an NVMe or SATA/USB SSD does not.
    """
    device = ""
    for partition in psutil.disk_partitions():
        if partition.mountpoint == "/":
            device = partition.device
            break
    name = device.rsplit("/", 1)[-1]
    if name.startswith("mmcblk"):
        return "SD"
    return "SSD"


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


def format_fan(rpm):
    """Fan speed with its unit, e.g. ``"2400 rpm"`` — always shown, even ``"0 rpm"``.

    An unreadable reading (``None``) collapses to ``"--"``. A board with no fan at all is
    handled upstream: :func:`collect_metrics` leaves ``fan`` empty so the row is hidden.
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
    cpu_temp = format_temperature(read_temp_c(sensors.cpu_temp), unit=False)
    ssd_free = format_bytes(disk.free)
    ssd_total = format_bytes(disk.total)
    ssd_temp = format_temperature(read_temp_c(sensors.ssd_temp), unit=False)
    net = read_network()
    # No fan sensor → no Fan row at all (fan stays None); a present sensor always shows the
    # row, even at "0 rpm".
    if sensors.fan is None:
        fan = None
    else:
        fan = format_fan(read_fan_rpm(sensors.fan))
    return Metrics(
        hostname=hostname,
        ip=or_missing(read_primary_ip()),
        net_kind=net.kind,
        ssid=or_missing(net.ssid),
        wifi_signal=net.signal,
        cpu=f"{cpu_load} / {cpu_temp}",
        ram=format_ram(vm.total - vm.available, vm.total),
        disk_label=read_disk_label(),
        ssd=f"{ssd_free}/{ssd_total} {ssd_temp}",
        fan=fan,
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


def run(display, args, hostname, sensors, services, config_store):
    """Draw a single frame (``--once``) or loop, alternating pages, until interrupted."""
    if args.once:
        # A short pause so the first cpu_percent reading is meaningful.
        time.sleep(0.7)
        show_dashboard(display, collect_metrics(hostname, sensors),
                       config_store.snapshot().rows)
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

            _show_current_page(display, args, hostname, sensors, services, config_store)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def _show_current_page(display, args, hostname, sensors, services, config_store):
    """Draw whichever page is due now — the dashboard, or an optional page on its turn.

    Page 0 is always the dashboard. The weather and indoor-meteo pages join the rotation only
    when their background service is running AND the config keeps them enabled, so the web panel
    can turn either off independently. Pages alternate on an ``args.page_seconds`` timer. The
    dashboard reads its row layout from the shared config each frame, so a web-panel edit shows
    up on the next redraw without a restart.
    """
    config = config_store.snapshot()

    # Page 0 is the dashboard; append each optional page that is both live and enabled.
    pages = ["dashboard"]
    if services.weather is not None and config.weather.enabled:
        pages.append("weather")
    if services.meteo is not None and config.meteo.enabled:
        pages.append("meteo")

    index = due_page_index(time.monotonic(), seconds=args.page_seconds, count=len(pages))
    page = pages[index]
    if page == "weather":
        show_weather(display, services.weather.latest())
    elif page == "meteo":
        show_meteo(display, services.meteo.latest())
    else:
        show_dashboard(display, collect_metrics(hostname, sensors), config.rows)


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
                        help="redraw period in seconds (default 5)")
    parser.add_argument("--once", action="store_true",
                        help="draw a single frame and exit (the frame stays on screen)")

    # Weather page (a second screen the loop alternates with the dashboard).
    parser.add_argument("--weather", action=argparse.BooleanOptionalAction, default=True,
                        help="show the weather page in the rotation (default on; "
                             "--no-weather to disable)")
    parser.add_argument("--page-seconds", type=float, default=15.0,
                        help="seconds each page is shown before switching (default 15)")
    parser.add_argument("--weather-refresh", type=float, default=1800.0,
                        help="how often to refetch the weather, in seconds (default 1800)")
    parser.add_argument("--weather-timeout", type=float, default=6.0,
                        help="network timeout for weather requests, in seconds (default 6)")
    parser.add_argument("--latitude", type=float, default=None,
                        help="fixed latitude (with --longitude) instead of IP geolocation")
    parser.add_argument("--longitude", type=float, default=None,
                        help="fixed longitude (with --latitude) instead of IP geolocation")
    parser.add_argument("--city", type=str, default=None,
                        help="place name to show (default: from geolocation)")

    # Indoor meteo page (a third screen: the local AHT20+BMP280 sensor readings).
    parser.add_argument("--meteo", action=argparse.BooleanOptionalAction, default=True,
                        help="show the indoor sensor page (AHT20+BMP280) in the rotation "
                             "(default on; --no-meteo to disable)")
    parser.add_argument("--meteo-refresh", type=float, default=30.0,
                        help="how often to read the indoor sensors, in seconds (default 30)")

    # Runtime config file + web panel. The --city/--latitude/--longitude and --weather flags
    # above only seed the config on first run; after that config.json (edited via the panel)
    # wins.
    parser.add_argument("--config", type=str,
                        default=str(Path(__file__).resolve().parent / "config.json"),
                        help="path to the runtime config JSON (default: next to this script)")
    parser.add_argument("--web", action=argparse.BooleanOptionalAction, default=True,
                        help="serve the web config panel (default on; --no-web to disable)")
    parser.add_argument("--web-port", type=int, default=webconfig.DEFAULT_PORT,
                        help=f"web panel TCP port (default {webconfig.DEFAULT_PORT})")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.night_contrast is not None and args.contrast is None:
        raise SystemExit("--night-contrast needs --contrast (the daytime brightness) to "
                         "restore to when the night window ends")

    # SIGTERM (systemctl stop / shutdown) should blank the panel like Ctrl-C, so the last
    # frame is not left burning in while the Pi is off but still on mains.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    config_store = _load_config(args)

    hostname = socket.gethostname()
    sensors = Sensors(
        cpu_temp=find_hwmon_input("cpu_thermal", "temp1_input"),
        ssd_temp=find_hwmon_input("nvme", "temp1_input"),
        fan=find_hwmon_input("pwmfan", "fan1_input"),
    )

    # Prime psutil.cpu_percent, otherwise the first reading returns 0.
    psutil.cpu_percent(interval=None)

    weather_service = _start_weather(args, config_store.snapshot().weather)
    meteo_service = _start_meteo(args)
    services = Services(weather=weather_service, meteo=meteo_service)
    if args.web and not args.once:
        # A failure to bind the panel (e.g. the port is taken) must not stop the display —
        # log it and carry on showing stats.
        try:
            _start_web(args, config_store, weather_service)
        except OSError as error:
            print(f"webconfig: could not start on port {args.web_port}: {error}", flush=True)

    # In --once mode keep the frame on screen; in the loop, clear it on exit
    # (Ctrl-C or SIGTERM).
    with OledDisplay.open(bus=args.bus, address=args.address, rotate=args.rotate,
                          contrast=args.contrast, clear_on_close=not args.once) as display:
        run(display, args, hostname, sensors, services, config_store)


def _load_config(args):
    """Load the runtime config (seeded from the CLI args on first run) into a store."""
    seed = config.default_seed(
        city=args.city,
        latitude=args.latitude,
        longitude=args.longitude,
        weather_enabled=args.weather,
        meteo_enabled=args.meteo,
    )
    store = config.ConfigStore(args.config, seed=seed,
                               log=lambda message: print(message, flush=True))
    store.load_or_seed()
    return store


def _start_weather(args, weather_conf):
    """Build and start the weather service, or return ``None`` when weather is off.

    The location comes from the runtime config: a manual place with coordinates skips IP
    geolocation, anything else resolves by IP on the service's own thread. The service runs
    whenever ``--weather`` is on even if the config currently hides the page — so re-enabling
    it from the panel shows fresh data at once. Weather is never fetched in ``--once``.
    """
    if not args.weather or args.once:
        return None

    location = None
    if (weather_conf.mode == config.WEATHER_MANUAL
            and weather_conf.latitude is not None and weather_conf.longitude is not None):
        location = weather.Location(latitude=weather_conf.latitude,
                                    longitude=weather_conf.longitude,
                                    city=weather_conf.city or "--")

    service = weather.WeatherService(
        refresh_seconds=args.weather_refresh,
        timeout=args.weather_timeout,
        location=location,
        log=lambda message: print(message, flush=True),
    )
    service.start()
    return service


def _start_meteo(args):
    """Build and start the indoor sensor service, or return ``None`` when meteo is off / --once.

    Reads the AHT20+BMP280 on the same I²C bus as the display every ``--meteo-refresh`` seconds
    on its own thread; the display loop reads the cached snapshot via ``latest()`` and never
    blocks on a sensor measurement. Runs whenever ``--meteo`` is on even if the config currently
    hides the page, so re-enabling it from the panel shows a fresh reading at once. Sensors are
    never read in ``--once``.
    """
    if not args.meteo or args.once:
        return None

    service = local_meteo.MeteoService(
        refresh_seconds=args.meteo_refresh,
        bus_number=args.bus,
        log=lambda message: print(message, flush=True),
    )
    service.start()
    return service


def _start_web(args, config_store, weather_service):
    """Start the web config panel on its daemon thread."""
    server = webconfig.WebConfigServer(
        config_store=config_store,
        weather_service=weather_service,
        port=args.web_port,
        timeout=args.weather_timeout,
        log=lambda message: print(message, flush=True),
    )
    server.start()


if __name__ == "__main__":
    main()
