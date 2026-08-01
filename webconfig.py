#!/usr/bin/env python3
"""A tiny web panel for configuring the OLED dashboard, live and without a restart.

Runs an :class:`http.server.ThreadingHTTPServer` on a daemon thread — the same
"background helper" pattern as :class:`weather.WeatherService` — so it lives beside the
display loop inside the one dashboard process. It serves a single self-contained HTML page
(inline CSS + vanilla JS, no external assets) and a small JSON API:

    GET  /                — the config page
    GET  /api/config      — the current config (with row labels for the UI)
    POST /api/config      — validate and persist edits (rows + weather + meteo + the MQTT toggle)
    GET  /api/geocode?q=  — Open-Meteo geocoding matches for a typed city

Edits are applied through the shared :class:`config.ConfigStore` (the render loop reads it
each frame, so a layout change shows up on the next redraw) and, when the weather location
changes, pushed straight into the running :class:`weather.WeatherService` so the new city
is fetched immediately. Only the standard library is used, so the deploy gains no
dependency. The panel binds to all interfaces for LAN use and has no authentication — it is
meant for a trusted home network.
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import weather

HOST = "0.0.0.0"
DEFAULT_PORT = 8080


def _config_for_ui(cfg):
    """Shape a :class:`config.Config` for the page: the persisted shape plus row labels.

    The layout rows carry their human label and note (from :data:`config.ROW_DEFS`) so the
    page can render them without a second request; the POST back only needs ``key`` and
    ``enabled``, and :func:`config.parse_config` ignores the extra fields.
    """
    label_by_key = {row.key: row for row in config.ROW_DEFS}
    rows = []
    for row in cfg.rows:
        definition = label_by_key.get(row.key)
        rows.append({
            "key": row.key,
            "enabled": row.enabled,
            "label": definition.label if definition else row.key,
            "note": definition.note if definition else "",
        })
    return {
        "layout": {"rows": rows},
        "weather": {
            "enabled": cfg.weather.enabled,
            "mode": cfg.weather.mode,
            "city": cfg.weather.city,
            "latitude": cfg.weather.latitude,
            "longitude": cfg.weather.longitude,
        },
        "meteo": {
            "enabled": cfg.meteo.enabled,
            "temp_offset": cfg.meteo.temp_offset,
            # The picker bounds/step travel with the config so the page builds the options from a
            # single source of truth (config.py) rather than hard-coding the range in the HTML.
            "temp_offset_min": config.METEO_TEMP_OFFSET_MIN,
            "temp_offset_max": config.METEO_TEMP_OFFSET_MAX,
            "temp_offset_step": config.METEO_TEMP_OFFSET_STEP,
        },
        # Only the on/off toggle is exposed — never the broker host/credentials (a secret the
        # browser must not see). ``configured`` lets the page hint when the toggle would do nothing
        # because config.json has no broker address yet.
        "mqtt": {
            "enabled": cfg.mqtt.enabled,
            "configured": bool(cfg.mqtt.host),
        },
    }


class WebConfigServer:
    """Owns the HTTP server and starts it on a daemon thread.

    Holds the shared :class:`config.ConfigStore` and the optional
    :class:`weather.WeatherService` so request handlers can read and apply edits live.
    """

    def __init__(self, *, config_store, weather_service, port=DEFAULT_PORT, timeout=6.0,
                 log=print):
        self._config_store = config_store
        self._weather_service = weather_service
        self._port = port
        self._timeout = timeout
        self._log = log

    def start(self):
        """Launch the server on a daemon thread and return the thread."""
        server = _ConfigHTTPServer(
            (HOST, self._port), _Handler,
            config_store=self._config_store,
            weather_service=self._weather_service,
            timeout=self._timeout,
            log=self._log,
        )
        thread = threading.Thread(target=server.serve_forever, name="webconfig", daemon=True)
        thread.start()
        self._log(f"webconfig: serving on http://{HOST}:{self._port}")
        return thread


class _ConfigHTTPServer(ThreadingHTTPServer):
    """A threading HTTP server carrying the shared state its handlers need."""

    daemon_threads = True

    def __init__(self, address, handler, *, config_store, weather_service, timeout, log):
        super().__init__(address, handler)
        self.config_store = config_store
        self.weather_service = weather_service
        self.timeout_seconds = timeout
        self.log = log


class _Handler(BaseHTTPRequestHandler):
    """Routes the handful of endpoints; every handler is guarded so one bad request can't
    take the server (or the display process) down."""

    def do_GET(self):
        route = urllib.parse.urlparse(self.path)
        try:
            if route.path in ("/", "/index.html"):
                self._send_html(PAGE_HTML)
            elif route.path == "/api/config":
                self._send_json(_config_for_ui(self.server.config_store.snapshot()))
            elif route.path == "/api/geocode":
                self._handle_geocode(route)
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as error:   # never let a handler bubble up and kill the thread
            self.server.log(f"webconfig: GET {route.path} failed: {error}")
            self._send_json({"error": str(error)}, status=500)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path)
        try:
            if route.path == "/api/config":
                self._handle_save()
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as error:
            self.server.log(f"webconfig: POST {route.path} failed: {error}")
            self._send_json({"error": str(error)}, status=500)

    def _handle_geocode(self, route):
        """Look up a city name and return the matches for the picker."""
        params = urllib.parse.parse_qs(route.query)
        query = (params.get("q") or [""])[0]
        matches = weather.geocode_city(query, timeout=self.server.timeout_seconds)
        results = [{
            "name": match.name,
            "country": match.country,
            "admin1": match.admin1,
            "latitude": match.latitude,
            "longitude": match.longitude,
        } for match in matches]
        self._send_json({"results": results})

    def _handle_save(self):
        """Validate + persist the posted config, then push a location change to weather."""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        try:
            raw = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            self._send_json({"error": "invalid JSON"}, status=400)
            return

        before = self.server.config_store.snapshot()
        after = self.server.config_store.save_from_raw(raw)
        self._apply_weather_change(before.weather, after.weather)
        self._send_json(_config_for_ui(after))

    def _apply_weather_change(self, before, after):
        """Re-point the weather service only when the location actually changed.

        Toggling the weather page on/off, or reordering rows (which posts the whole config),
        must not needlessly drop the cached reading — only a real location change does.
        """
        location_before = (before.mode, before.city, before.latitude, before.longitude)
        location_after = (after.mode, after.city, after.latitude, after.longitude)
        if location_before == location_after:
            return

        service = self.server.weather_service
        if service is None:
            return
        if after.mode == config.WEATHER_AUTO:
            service.set_location(None)   # back to IP geolocation
        elif after.latitude is not None and after.longitude is not None:
            service.set_location(weather.Location(
                latitude=after.latitude,
                longitude=after.longitude,
                city=after.city or "--",
            ))
        self.server.log(f"webconfig: weather location -> {after.mode} {after.city}")

    # --- Response helpers ---------------------------------------------------

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        """Route request logging through the injected logger (keeps it in the service log)."""
        self.server.log("webconfig: " + (fmt % args))


# --- The page (self-contained: inline CSS + vanilla JS, no external assets) --
PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OLED dashboard config</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; padding: 24px 16px 48px;
    font: 16px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: #14171c; color: #e6e9ef;
    max-width: 640px; margin-inline: auto;
    overflow-wrap: anywhere;
  }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 0 0 12px; color: #aab2c0; text-transform: uppercase; letter-spacing: .04em; }
  section { background: #1b1f26; border: 1px solid #272c35; border-radius: 12px; padding: 16px; margin: 18px 0; }
  .hint { color: #8b93a3; font-size: 13px; margin: 0 0 12px; }
  ul { list-style: none; margin: 0; padding: 0; }
  .rows li {
    display: flex; align-items: center; gap: 10px;
    padding: 10px; border: 1px solid #272c35; border-radius: 10px; margin-bottom: 8px;
    background: #20252e;
  }
  .rows li.off { opacity: .5; }
  .rows .label { flex: 1; }
  .rows .label small { display: block; color: #8b93a3; font-size: 12px; }
  .rows input[type=checkbox] { width: 20px; height: 20px; }
  button {
    font: inherit; color: #e6e9ef; background: #2c333f; border: 1px solid #3a424f;
    border-radius: 8px; padding: 6px 12px; cursor: pointer;
  }
  button:hover:not(:disabled) { background: #353d4b; }
  button:disabled { opacity: .35; cursor: default; }
  .arrows button { padding: 4px 10px; font-size: 15px; }
  label.check { display: flex; align-items: center; gap: 10px; margin: 10px 0; }
  label.check input { width: 20px; height: 20px; }
  .field { display: flex; gap: 8px; margin: 12px 0 8px; }
  .field input[type=text], .field select {
    flex: 1; font: inherit; color: #e6e9ef; background: #20252e;
    border: 1px solid #3a424f; border-radius: 8px; padding: 8px 10px;
  }
  .field-label { display: block; font-size: 14px; color: #aab2c0; margin: 14px 0 0; }
  .results li { padding: 8px 10px; border: 1px solid #272c35; border-radius: 8px; margin-bottom: 6px; cursor: pointer; }
  .results li:hover { background: #262c36; }
  #wx-current { color: #9fd0a0; font-size: 13px; }
  .save { display: block; width: 100%; padding: 12px; font-size: 16px; background: #2f6d4f; border-color: #3a8560; }
  .save:hover:not(:disabled) { background: #367a58; }
  #status { min-height: 20px; font-size: 14px; margin: 8px 0 0; }
  #status.ok { color: #9fd0a0; }
  #status.err { color: #e39191; }

  /* Phone-sized screens: tighter margins, bigger touch targets, no sideways scroll. */
  @media (max-width: 480px) {
    body { padding: 16px 12px 32px; }
    h1 { font-size: 20px; }
    section { padding: 14px; margin: 14px 0; border-radius: 10px; }
    .rows li { padding: 8px; gap: 8px; }
    .rows input[type=checkbox], label.check input { width: 24px; height: 24px; }
    .arrows button { padding: 8px 12px; font-size: 17px; }
    button { padding: 9px 12px; }
    .field { flex-wrap: wrap; }
    .field input[type=text] { min-width: 0; }
    .save { padding: 14px; }
  }
</style>
</head>
<body>
  <h1>OLED dashboard</h1>
  <p class="hint">Changes apply live on the next screen redraw.</p>

  <section>
    <h2>System info</h2>
    <p class="hint">Tick a row to show it. Reorder with the arrows &mdash; the top row is drawn first.</p>
    <ul id="rows" class="rows"></ul>
  </section>

  <section>
    <h2>Weather</h2>
    <label class="check"><input type="checkbox" id="wx-enabled"> Show the weather screen</label>
    <label class="check"><input type="checkbox" id="wx-auto"> Locate automatically (by IP)</label>
    <div id="wx-manual">
      <div class="field">
        <input type="text" id="wx-city" placeholder="City name, e.g. Rijeka" autocomplete="off">
        <button id="wx-search" type="button">Search</button>
      </div>
      <ul id="wx-results" class="results"></ul>
      <p id="wx-current"></p>
    </div>
  </section>

  <section>
    <h2>Local meteo</h2>
    <label class="check"><input type="checkbox" id="mt-enabled"> Show the indoor sensor screen</label>
    <p class="hint">Room temperature, humidity and pressure from the AHT20 + BMP280 sensor.</p>
    <label class="field-label" for="mt-offset">Temperature compensation</label>
    <div class="field">
      <select id="mt-offset"></select>
    </div>
    <p class="hint">Added to the displayed room temperature — the sensor sits close to the panel and board, so it reads a little high.</p>
  </section>

  <section>
    <h2>Home Assistant</h2>
    <label class="check"><input type="checkbox" id="mq-enabled"> Publish sensor readings over MQTT</label>
    <p class="hint">Sends room temperature, humidity and pressure to Home Assistant via the MQTT broker. The broker address and credentials are set in <code>config.json</code> on the device.</p>
    <p id="mq-warn" class="hint" style="display:none; color:#e3b591;">No broker configured in <code>config.json</code> — the toggle has no effect until it is set.</p>
  </section>

  <button id="save" class="save" type="button">Save</button>
  <p id="status"></p>

<script>
"use strict";

// Live UI state, seeded from GET /api/config and posted back on Save.
const state = { rows: [], weather: {}, meteo: {}, mqtt: {} };

function setStatus(text, kind) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.className = kind || "";
}

async function loadConfig() {
  const res = await fetch("/api/config");
  const data = await res.json();
  state.rows = data.layout.rows;
  state.weather = data.weather;
  state.meteo = data.meteo;
  state.mqtt = data.mqtt;
  renderRows();
  renderWeather();
  renderMeteo();
  renderMqtt();
}

// --- System info rows ------------------------------------------------------

function renderRows() {
  const list = document.getElementById("rows");
  list.textContent = "";
  state.rows.forEach((row, index) => list.appendChild(buildRow(row, index)));
}

function buildRow(row, index) {
  const li = document.createElement("li");
  if (!row.enabled) li.classList.add("off");

  const check = document.createElement("input");
  check.type = "checkbox";
  check.checked = row.enabled;
  check.addEventListener("change", () => {
    state.rows[index].enabled = check.checked;
    renderRows();
  });

  const label = document.createElement("div");
  label.className = "label";
  label.textContent = row.label;
  if (row.note) {
    const note = document.createElement("small");
    note.textContent = row.note;
    label.appendChild(note);
  }

  const arrows = document.createElement("div");
  arrows.className = "arrows";
  arrows.appendChild(arrowButton("↑", index, -1));
  arrows.appendChild(arrowButton("↓", index, +1));

  li.appendChild(check);
  li.appendChild(label);
  li.appendChild(arrows);
  return li;
}

function arrowButton(glyph, index, delta) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = glyph;
  const target = index + delta;
  button.disabled = target < 0 || target >= state.rows.length;
  button.addEventListener("click", () => moveRow(index, target));
  return button;
}

function moveRow(from, to) {
  const rows = state.rows;
  const moved = rows.splice(from, 1)[0];
  rows.splice(to, 0, moved);
  renderRows();
}

// --- Weather ---------------------------------------------------------------

function renderWeather() {
  document.getElementById("wx-enabled").checked = !!state.weather.enabled;
  const auto = state.weather.mode === "auto";
  document.getElementById("wx-auto").checked = auto;
  document.getElementById("wx-city").value = state.weather.city || "";
  toggleManual(auto);
  renderCurrentLocation();
}

function toggleManual(auto) {
  document.getElementById("wx-manual").style.display = auto ? "none" : "block";
}

function renderCurrentLocation() {
  const el = document.getElementById("wx-current");
  const w = state.weather;
  if (w.mode === "manual" && w.latitude != null && w.longitude != null) {
    el.textContent = "Current: " + (w.city || "?") +
      " (" + w.latitude.toFixed(3) + ", " + w.longitude.toFixed(3) + ")";
  } else {
    el.textContent = "";
  }
}

async function searchCity() {
  const query = document.getElementById("wx-city").value.trim();
  const results = document.getElementById("wx-results");
  results.textContent = "";
  if (!query) return;
  setStatus("Searching…", "");
  try {
    const res = await fetch("/api/geocode?q=" + encodeURIComponent(query));
    const data = await res.json();
    if (data.error) { setStatus("Search failed: " + data.error, "err"); return; }
    if (!data.results.length) { setStatus("No matches for “" + query + "”", "err"); return; }
    setStatus("", "");
    data.results.forEach((match) => results.appendChild(buildResult(match)));
  } catch (error) {
    setStatus("Search failed: " + error, "err");
  }
}

function buildResult(match) {
  const li = document.createElement("li");
  const place = [match.name, match.admin1, match.country].filter(Boolean).join(", ");
  li.textContent = place + "  (" + match.latitude.toFixed(3) + ", " + match.longitude.toFixed(3) + ")";
  li.addEventListener("click", () => {
    state.weather.city = match.name;
    state.weather.latitude = match.latitude;
    state.weather.longitude = match.longitude;
    document.getElementById("wx-city").value = match.name;
    document.getElementById("wx-results").textContent = "";
    renderCurrentLocation();
    setStatus("Picked " + place + " — press Save to apply", "ok");
  });
  return li;
}

// --- Local meteo -----------------------------------------------------------

function renderMeteo() {
  document.getElementById("mt-enabled").checked = !!state.meteo.enabled;
  renderOffsetOptions();
}

// Build the compensation picker from the range/step the server sent, selecting the saved offset.
function renderOffsetOptions() {
  const select = document.getElementById("mt-offset");
  const min = state.meteo.temp_offset_min;
  const max = state.meteo.temp_offset_max;
  const step = state.meteo.temp_offset_step > 0 ? state.meteo.temp_offset_step : 0.5;
  const current = state.meteo.temp_offset || 0;
  select.textContent = "";
  for (let value = min; value <= max + 1e-9; value += step) {
    const rounded = Math.round(value / step) * step;
    const option = document.createElement("option");
    option.value = String(rounded);
    option.textContent = formatOffset(rounded);
    if (Math.abs(rounded - current) < 1e-9) option.selected = true;
    select.appendChild(option);
  }
}

function formatOffset(value) {
  if (value === 0) return "0.0 °C (no change)";
  const prefix = value > 0 ? "+" : "";
  return prefix + value.toFixed(1) + " °C";
}

// --- Home Assistant (MQTT) -------------------------------------------------

function renderMqtt() {
  document.getElementById("mq-enabled").checked = !!state.mqtt.enabled;
  // Hint when the toggle can't do anything because no broker is set in config.json.
  document.getElementById("mq-warn").style.display = state.mqtt.configured ? "none" : "block";
}

// --- Save ------------------------------------------------------------------

function collectPayload() {
  const auto = document.getElementById("wx-auto").checked;
  return {
    layout: {
      rows: state.rows.map((row) => ({ key: row.key, enabled: row.enabled })),
    },
    weather: {
      enabled: document.getElementById("wx-enabled").checked,
      mode: auto ? "auto" : "manual",
      city: state.weather.city || "",
      latitude: state.weather.latitude,
      longitude: state.weather.longitude,
    },
    meteo: {
      enabled: document.getElementById("mt-enabled").checked,
      temp_offset: parseFloat(document.getElementById("mt-offset").value),
    },
    // Only the toggle — the broker host/credentials stay server-side in config.json and are
    // preserved across saves by ConfigStore.save_from_raw.
    mqtt: {
      enabled: document.getElementById("mq-enabled").checked,
    },
  };
}

async function save() {
  const button = document.getElementById("save");
  button.disabled = true;
  setStatus("Saving…", "");
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectPayload()),
    });
    const data = await res.json();
    if (data.error) { setStatus("Save failed: " + data.error, "err"); return; }
    state.rows = data.layout.rows;
    state.weather = data.weather;
    state.meteo = data.meteo;
    state.mqtt = data.mqtt;
    renderRows();
    renderWeather();
    renderMeteo();
    renderMqtt();
    setStatus("Saved. The screen updates on its next redraw.", "ok");
  } catch (error) {
    setStatus("Save failed: " + error, "err");
  } finally {
    button.disabled = false;
  }
}

document.getElementById("wx-auto").addEventListener("change", (event) => {
  toggleManual(!event.target.checked);
});
document.getElementById("wx-search").addEventListener("click", searchCity);
document.getElementById("wx-city").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); searchCity(); }
});
document.getElementById("save").addEventListener("click", save);

loadConfig().catch((error) => setStatus("Could not load config: " + error, "err"));
</script>
</body>
</html>
"""
