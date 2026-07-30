#!/usr/bin/env python3
"""Weather data for the OLED weather page.

Fetches the current conditions and the day's high/low from **Open-Meteo** (a free,
key-less public API) for a location resolved once by IP geolocation. A background thread
keeps a single cached :class:`WeatherData` fresh on a slow timer, so the display loop
never blocks on the network — it just reads the latest snapshot via :meth:`WeatherService.latest`
(which is ``None`` until the first successful fetch).

Only the standard library is used (``urllib``), so the deploy needs no extra dependency.
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from collections import namedtuple

# Endpoints — all key-less and free.
GEO_IP_URL = "https://ipapi.co/json/"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "rpi5-oled-dashboard (+https://github.com/Jacksotnik/rpi5-oled-dashboard)"

# How quickly to retry when we still have nothing to show (a slow-path startup failure
# should recover in a minute, not wait a full refresh period).
RETRY_SECONDS = 60

# Weather categories the icon drawer knows how to render.
CLEAR = "clear"
PARTLY = "partly"
CLOUDY = "cloudy"
FOG = "fog"
RAIN = "rain"
SNOW = "snow"
THUNDER = "thunder"

# A resolved place, the latest reading for it, and a geocoding search hit.
Location = namedtuple("Location", "latitude longitude city")
WeatherData = namedtuple("WeatherData", "temp high low category is_day city updated")
GeoResult = namedtuple("GeoResult", "name country admin1 latitude longitude")


def category_for_code(code):
    """Map a WMO weather code to one of the drawable categories (the constants above)."""
    if code == 0:
        return CLEAR
    if code in (1, 2):
        return PARTLY
    if code == 3:
        return CLOUDY
    if code in (45, 48):
        return FOG
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return RAIN
    if code in (71, 73, 75, 77, 85, 86):
        return SNOW
    if code in (95, 96, 99):
        return THUNDER
    return CLOUDY   # unknown code → a neutral cloud


def _get_json(url, *, timeout):
    """GET ``url`` and parse its JSON body (raises on a network or parse error)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _as_float(value):
    """Best-effort float conversion, or ``None`` for a missing/garbage value."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_location(*, timeout):
    """Resolve this host's approximate location by IP as a :class:`Location` (raises on failure)."""
    data = _get_json(GEO_IP_URL, timeout=timeout)
    latitude = _as_float(data.get("latitude"))
    longitude = _as_float(data.get("longitude"))
    if latitude is None or longitude is None:
        raise ValueError("IP geolocation returned no coordinates")
    city = str(data.get("city") or "--")
    return Location(latitude=latitude, longitude=longitude, city=city)


def geocode_city(name, *, timeout, count=5, language="en"):
    """Look up a place ``name`` via Open-Meteo's geocoding API → a list of :class:`GeoResult`.

    Used by the web panel to turn a typed city name into the latitude/longitude the forecast
    API needs. Returns ``[]`` for a blank name or no matches; raises on a network/parse error
    (the caller turns that into an error response).
    """
    query = name.strip()
    if not query:
        return []
    url = f"{GEOCODE_URL}?" + urllib.parse.urlencode({
        "name": query, "count": count, "language": language, "format": "json",
    })
    data = _get_json(url, timeout=timeout)
    results = data.get("results") or []
    matches = []
    for item in results:
        latitude = _as_float(item.get("latitude"))
        longitude = _as_float(item.get("longitude"))
        if latitude is None or longitude is None:
            continue
        matches.append(GeoResult(
            name=str(item.get("name") or query),
            country=str(item.get("country") or ""),
            admin1=str(item.get("admin1") or ""),
            latitude=latitude,
            longitude=longitude,
        ))
    return matches


def fetch_weather(location, *, timeout, now):
    """Fetch current conditions + today's high/low for ``location`` as :class:`WeatherData`.

    ``now`` is the wall-clock epoch to stamp as the reading's ``updated`` time (passed in
    so the caller owns the clock and this stays easy to test).
    """
    query = urllib.parse.urlencode({
        "latitude": location.latitude,
        "longitude": location.longitude,
        "current": "temperature_2m,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": 1,
    })
    data = _get_json(f"{FORECAST_URL}?{query}", timeout=timeout)

    current = data.get("current", {})
    daily = data.get("daily", {})
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    code = current.get("weather_code")

    return WeatherData(
        temp=_as_float(current.get("temperature_2m")),
        high=_as_float(highs[0]) if highs else None,
        low=_as_float(lows[0]) if lows else None,
        category=category_for_code(int(code) if code is not None else -1),
        is_day=int(current.get("is_day", 1)) == 1,
        city=location.city,
        updated=now,
    )


class WeatherService:
    """Keeps one cached :class:`WeatherData` fresh on a background thread.

    Resolves the location once (by IP, unless one was supplied), then re-fetches the
    weather every ``refresh_seconds``. The display loop calls :meth:`latest` (cheap,
    lock-guarded) and never blocks on the network; ``latest`` is ``None`` until the first
    successful fetch.
    """

    def __init__(self, *, refresh_seconds, timeout, location=None, log=print):
        self._refresh_seconds = refresh_seconds
        self._timeout = timeout
        self._location = location   # a pre-set Location skips IP geolocation
        self._log = log
        self._lock = threading.Lock()
        self._data = None
        # Set to interrupt the loop's wait so a location change refetches immediately.
        self._wake = threading.Event()

    def latest(self):
        """Return the most recent :class:`WeatherData`, or ``None`` if none yet."""
        with self._lock:
            return self._data

    def set_location(self, location):
        """Point the service at a new location and refetch right away.

        ``location`` is a fixed :class:`Location`, or ``None`` to switch back to IP
        geolocation (re-resolved on the next fetch). The cached reading is dropped so the
        display never shows the old city's data as if it were the new place, and the
        background thread is woken so the change lands without waiting for the next refresh.
        """
        with self._lock:
            self._location = location
            self._data = None
        self._wake.set()

    def _store(self, data):
        with self._lock:
            self._data = data

    def _refresh_once(self):
        """Resolve the location if needed, fetch the weather and cache it."""
        with self._lock:
            location = self._location
        if location is None:
            location = resolve_location(timeout=self._timeout)
            with self._lock:
                self._location = location
            self._log(f"weather: location resolved to {location.city} "
                      f"({location.latitude:.3f}, {location.longitude:.3f})")
        data = fetch_weather(location, timeout=self._timeout, now=time.time())
        self._store(data)
        self._log(f"weather: updated {data.city} — {data.temp}°C, {data.category}, "
                  f"day={data.is_day}")

    def _run(self):
        # A plain service loop: refresh, then wait. One failed cycle must not kill the
        # thread, so every fetch is guarded and merely logged. The wait is interruptible
        # (self._wake) so set_location can force an immediate refresh.
        while True:
            try:
                self._refresh_once()
            except Exception as error:   # network / parse / anything — stay alive
                self._log(f"weather: refresh failed: {error}")
            wait = self._refresh_seconds if self.latest() is not None else RETRY_SECONDS
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def start(self):
        """Launch the refresh loop on a daemon thread and return it."""
        thread = threading.Thread(target=self._run, name="weather", daemon=True)
        thread.start()
        return thread
