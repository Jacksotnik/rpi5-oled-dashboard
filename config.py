#!/usr/bin/env python3
"""Runtime configuration for the OLED dashboard, editable from the web panel.

The dashboard used to be steered entirely by CLI flags, with the system-info row layout
hard-coded in ``stats_oled.show_dashboard``. This module moves the *user-editable* subset —
which system-info rows to show and in what order, and the weather location — into a small
``config.json`` that both the display loop and the web panel share.

The single source of truth for *which* system-info rows exist and their human labels lives
here (:data:`ROW_DEFS`), so the web UI and the config validator agree without importing the
renderer (the renderer owns only *how* each row is drawn). Everything is read back through a
thread-safe :class:`ConfigStore`: the display loop takes a cheap :meth:`~ConfigStore.snapshot`
each frame, and the web panel writes edits through :meth:`~ConfigStore.save_from_raw`, which
validates loosely-typed input once for both file loads and web posts.

The JSON shape (``config.json``)::

    {
      "version": 1,
      "layout": { "rows": [ {"key": "cpu", "enabled": true}, ... ] },
      "weather": { "mode": "manual", "city": "Rijeka",
                   "latitude": 45.32673, "longitude": 14.44241 },
      "meteo": { "enabled": true, "temp_offset": 0.0 }
    }
"""

import json
import os
import threading
from collections import namedtuple
from pathlib import Path

CONFIG_VERSION = 1

# --- The system-info rows the dashboard can show ----------------------------
# The canonical set of configurable rows, in their default order. This is the source of
# truth shared by the web UI (labels, order) and the config validator (which keys are
# valid); the renderer keeps its own draw specs keyed by the same `key`. Editing this list
# is how a new row type is introduced — old config files gain it (appended, enabled) on the
# next load, and the web panel shows it automatically.
RowDef = namedtuple("RowDef", "key label note")
ROW_DEFS = [
    RowDef("cpu",  "CPU",              ""),
    RowDef("ram",  "RAM",              ""),
    RowDef("disk", "Disk (SSD / SD)",  ""),
    RowDef("net",  "Network (Wi-Fi / LAN)", ""),
    RowDef("ip",   "IP address",       ""),
    RowDef("fan",  "Fan",              "hidden when the board has no fan"),
]
ROW_KEYS = [row.key for row in ROW_DEFS]

# Weather location modes.
WEATHER_MANUAL = "manual"   # a fixed, geocoded place (city + latitude/longitude)
WEATHER_AUTO = "auto"       # resolve the location by IP geolocation, as before
WEATHER_MODES = (WEATHER_MANUAL, WEATHER_AUTO)

# Local meteo temperature compensation. The AHT20 sits close to the OLED panel and the RPi
# board, so it reads a little high; the user nudges the *displayed* room temperature by a fixed
# offset (°C). The web picker offers this range in these steps; the validator clamps + snaps to
# match, so a hand-edited config can't push the offset out of bounds.
METEO_TEMP_OFFSET_MIN = -5.0
METEO_TEMP_OFFSET_MAX = 5.0
METEO_TEMP_OFFSET_STEP = 0.5
METEO_TEMP_OFFSET_DEFAULT = 0.0

# --- Typed config -----------------------------------------------------------
# Loosely-typed JSON is parsed into these at the boundary; the rest of the app works with
# the typed objects and never reaches back into raw dicts.
LayoutRow = namedtuple("LayoutRow", "key enabled")
WeatherConf = namedtuple("WeatherConf", "enabled mode city latitude longitude")
MeteoConf = namedtuple("MeteoConf", "enabled temp_offset")   # the local AHT20+BMP280 sensor page
Config = namedtuple("Config", "rows weather meteo")   # rows: list[LayoutRow]


# --- Defensive readers for loosely-typed (JSON) data ------------------------

def _get(mapping, key, default):
    """Read ``key`` from a possibly-not-a-dict ``mapping`` with an explicit default."""
    if isinstance(mapping, dict) and key in mapping:
        return mapping[key]
    return default


def _as_bool(value, default):
    """Coerce a JSON value to bool, falling back to ``default`` for anything odd."""
    if isinstance(value, bool):
        return value
    return default


def _as_str(value, default):
    """Coerce a JSON value to a stripped string, or ``default`` if empty/missing."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _as_float_or_none(value):
    """Best-effort float, or ``None`` for a missing/garbage value."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_offset(value, default):
    """Coerce a JSON value to a temperature-compensation offset (°C).

    Clamps to ``[METEO_TEMP_OFFSET_MIN, METEO_TEMP_OFFSET_MAX]`` and snaps to
    ``METEO_TEMP_OFFSET_STEP`` so a hand-edited or stale config stays within what the picker can
    produce; anything missing/garbage falls back to ``default``.
    """
    number = _as_float_or_none(value)
    if number is None:
        return default
    clamped = max(METEO_TEMP_OFFSET_MIN, min(METEO_TEMP_OFFSET_MAX, number))
    steps = round(clamped / METEO_TEMP_OFFSET_STEP)
    return steps * METEO_TEMP_OFFSET_STEP


# --- Parsing / serialization ------------------------------------------------

def _parse_rows(raw_rows, *, seed_rows):
    """Validate the layout rows from raw JSON into an ordered list of :class:`LayoutRow`.

    Keeps only known row keys (:data:`ROW_KEYS`), in the order given, de-duplicated. Any
    known key missing from the input is appended, taking its enabled flag from ``seed_rows``
    (or visible by default), so a new row type introduced in code never silently vanishes.
    """
    seed_enabled = {row.key: row.enabled for row in seed_rows}
    rows = []
    seen = set()
    if isinstance(raw_rows, list):
        for entry in raw_rows:
            key = _as_str(_get(entry, "key", ""), "")
            if key not in ROW_KEYS or key in seen:
                continue
            enabled = _as_bool(_get(entry, "enabled", True), True)
            rows.append(LayoutRow(key=key, enabled=enabled))
            seen.add(key)
    for key in ROW_KEYS:
        if key not in seen:
            rows.append(LayoutRow(key=key, enabled=seed_enabled.get(key, True)))
    return rows


def _parse_weather(raw_weather, *, seed_weather):
    """Validate the weather block from raw JSON into a :class:`WeatherConf`.

    ``enabled`` toggles the whole weather page in the rotation. ``mode`` must be one of
    :data:`WEATHER_MODES` (else it falls back to the seed's mode). In manual mode coordinates
    are required for the location to be usable; if they are missing we keep whatever the seed
    had rather than inventing a place.
    """
    enabled = _as_bool(_get(raw_weather, "enabled", seed_weather.enabled), seed_weather.enabled)
    mode = _as_str(_get(raw_weather, "mode", ""), seed_weather.mode)
    if mode not in WEATHER_MODES:
        mode = seed_weather.mode
    city = _as_str(_get(raw_weather, "city", ""), seed_weather.city)
    latitude = _as_float_or_none(_get(raw_weather, "latitude", None))
    longitude = _as_float_or_none(_get(raw_weather, "longitude", None))
    if mode == WEATHER_MANUAL and (latitude is None or longitude is None):
        latitude = seed_weather.latitude
        longitude = seed_weather.longitude
    return WeatherConf(enabled=enabled, mode=mode, city=city,
                       latitude=latitude, longitude=longitude)


def _parse_meteo(raw_meteo, *, seed_meteo):
    """Validate the meteo block from raw JSON into a :class:`MeteoConf`.

    ``enabled`` toggles the whole indoor sensor page in the rotation; ``temp_offset`` shifts the
    displayed room temperature (°C) to compensate for the sensor's heat pickup near the board.
    Anything odd falls back to the seed's value.
    """
    enabled = _as_bool(_get(raw_meteo, "enabled", seed_meteo.enabled), seed_meteo.enabled)
    temp_offset = _as_offset(_get(raw_meteo, "temp_offset", seed_meteo.temp_offset),
                             seed_meteo.temp_offset)
    return MeteoConf(enabled=enabled, temp_offset=temp_offset)


def parse_config(raw, *, seed):
    """Parse a loosely-typed config dict into a validated :class:`Config`.

    Used for both the on-disk file and web-panel input, so validation lives in one place.
    ``seed`` supplies fallbacks for anything missing or malformed.
    """
    layout = _get(raw, "layout", {})
    rows = _parse_rows(_get(layout, "rows", None), seed_rows=seed.rows)
    weather = _parse_weather(_get(raw, "weather", {}), seed_weather=seed.weather)
    meteo = _parse_meteo(_get(raw, "meteo", {}), seed_meteo=seed.meteo)
    return Config(rows=rows, weather=weather, meteo=meteo)


def config_to_dict(config):
    """Serialize a :class:`Config` to the JSON-ready dict shape."""
    return {
        "version": CONFIG_VERSION,
        "layout": {
            "rows": [{"key": row.key, "enabled": row.enabled} for row in config.rows],
        },
        "weather": {
            "enabled": config.weather.enabled,
            "mode": config.weather.mode,
            "city": config.weather.city,
            "latitude": config.weather.latitude,
            "longitude": config.weather.longitude,
        },
        "meteo": {
            "enabled": config.meteo.enabled,
            "temp_offset": config.meteo.temp_offset,
        },
    }


def default_seed(*, city, latitude, longitude, weather_enabled, meteo_enabled):
    """Build the first-run seed config from the current CLI arguments.

    All rows start visible in their canonical order. The weather page starts enabled per the
    ``--weather/--no-weather`` flag, and its location is manual when a fixed
    latitude/longitude pair was supplied on the command line, otherwise auto (by IP). The
    indoor meteo page starts enabled per the ``--meteo/--no-meteo`` flag.
    """
    rows = [LayoutRow(key=key, enabled=True) for key in ROW_KEYS]
    has_coords = latitude is not None and longitude is not None
    weather = WeatherConf(
        enabled=weather_enabled,
        mode=WEATHER_MANUAL if has_coords else WEATHER_AUTO,
        city=city or "",
        latitude=latitude,
        longitude=longitude,
    )
    meteo = MeteoConf(enabled=meteo_enabled, temp_offset=METEO_TEMP_OFFSET_DEFAULT)
    return Config(rows=rows, weather=weather, meteo=meteo)


# --- Thread-safe store ------------------------------------------------------

class ConfigStore:
    """Holds the live :class:`Config`, persisted to ``path`` and shared across threads.

    The display loop reads with :meth:`snapshot` (cheap, lock-guarded) every frame; the web
    panel writes with :meth:`save_from_raw`. Writes are atomic (temp file + ``os.replace``)
    so a crash mid-write can never leave a half-written ``config.json`` on the panel's SD.
    """

    def __init__(self, path, *, seed, log=print):
        self._path = Path(path)
        self._seed = seed
        self._log = log
        self._lock = threading.Lock()
        self._config = seed

    def load_or_seed(self):
        """Load ``config.json`` if present, else write the seed and use it. Call once at startup."""
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text())
                config = parse_config(raw, seed=self._seed)
                with self._lock:
                    self._config = config
                self._log(f"config: loaded {self._path}")
                return config
            except (OSError, ValueError) as error:
                self._log(f"config: could not read {self._path} ({error}); using defaults")
                # Fall through to seeding so a corrupt file self-heals on next save.
        self._write(self._seed)
        with self._lock:
            self._config = self._seed
        self._log(f"config: seeded {self._path}")
        return self._seed

    def snapshot(self):
        """Return the current :class:`Config` (a cheap read for the render loop)."""
        with self._lock:
            return self._config

    def save_from_raw(self, raw):
        """Validate raw (web-posted) config, persist it, store it, and return the result.

        Validation reuses :func:`parse_config` against the seed, so bad input degrades to
        safe values instead of being rejected.
        """
        config = parse_config(raw, seed=self._seed)
        self._write(config)
        with self._lock:
            self._config = config
        self._log("config: saved from web panel")
        return config

    def _write(self, config):
        """Atomically write ``config`` to ``self._path`` (temp file in the same dir + replace)."""
        payload = json.dumps(config_to_dict(config), indent=2)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, self._path)
