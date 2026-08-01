# rpi5-oled-dashboard

System-stats display for a 1.5" **128×128** SH1107 OLED (I²C) on a Raspberry Pi 5. Shows a
header (hostname + uptime) and up to six rows: CPU (load / temp), RAM (used / total), the
root disk (free / total + temp, labelled `SSD` or `SD` after the actual medium), the
network uplink (Wi-Fi SSID with a signal-bars icon, or `LAN` on a wired link), IP, and —
when a fan is present — its rpm (the row is hidden on fanless boards). At night
(00:00–06:00 by default) the panel dims to slow OLED burn-in.

Every 15 seconds the screen alternates with a **weather page** — the current temperature
(°C), the day's high/low, a day/night condition icon, the time, date and place, and a
"last updated" footer. Weather comes from [Open-Meteo](https://open-meteo.com) (free, no
API key), refreshed every 30 minutes on a background thread — for a fixed location set on
the service command line, or (by default) one resolved automatically by IP.

A small built-in **web config panel** (see below) lets you pick which system-info rows to
show and reorder them, and set the weather city or turn the weather page off — all live,
without restarting the service. Those runtime choices live in a per-device `config.json`
(not in git); the command-line values only seed it on first run.

> Roadmap: system metrics today; hardware sensors and their readouts next.

The panel is driven by the **`oleddisplay`** library, installed into the venv from its own
repository — no copy is kept here:
<https://github.com/Jacksotnik/rpi5-sh1107-oled-128x128>

## Contents

| Path | Purpose |
|------|---------|
| `stats_oled.py` | the app — metric collection, screen layout, refresh loop, page rotation |
| `weather.py` | weather data layer: IP geolocation, geocoding + Open-Meteo fetch on a background thread |
| `config.py` | runtime config model + thread-safe store (`config.json`), shared by the loop and the panel |
| `local_meteo.py` | indoor AHT20 + BMP280 sensor service (I²C) + file-backed pressure history |
| `mqtt_publisher.py` | publishes the indoor readings to MQTT for Home Assistant (HA MQTT Discovery) |
| `webconfig.py` | the web config panel — a stdlib HTTP server on a daemon thread |
| `requirements.txt` | venv deps: the `oleddisplay` library (from git) + `psutil`, `smbus2`, `paho-mqtt` |
| `oled-stats.service` | systemd unit, installed to `/etc/systemd/system/` on the Pi |
| `install.sh` | one-command first install on the Pi |
| `update.sh` | one-command update / deploy on the Pi |

## Install (on the Pi)

```bash
git clone https://github.com/Jacksotnik/rpi5-oled-dashboard.git ~/oled-stats
~/oled-stats/install.sh
```

`install.sh` creates the venv, installs the dependencies, installs and enables the systemd
unit, and starts the service. Cloning a public repo is anonymous — no auth needed.

## Update / deploy

Push the change, then on the Pi run:

```bash
ssh rpi '~/oled-stats/update.sh'      # or ./update.sh while sshed in
```

`update.sh` does the whole deploy: `git pull`, sync venv deps, reinstall the `oleddisplay`
library only when its upstream commit moved (`--lib` forces it), reinstall the systemd unit
if it changed, restart the service, and tail the log.

> Changing the `oleddisplay` library? Push it in its own repo, then run `update.sh` on the
> Pi — it compares the installed commit with the repo HEAD and force-reinstalls when they
> differ (the version stays `0.1.0`, so plain pip would skip the new code).

## The service

`oled-stats.service` runs the app at boot and restarts it on failure:

```ini
[Service]
Type=simple
User=admin
WorkingDirectory=/home/admin/oled-stats
ExecStart=/home/admin/oled-stats/venv/bin/python /home/admin/oled-stats/stats_oled.py --interval 5 --rotate 3 --contrast 72 --night-contrast 16 --latitude 45.32673 --longitude 14.44241 --city Rijeka --web-port 8080
Restart=on-failure
RestartSec=3
```

- Runs as `admin` (in the `i2c` group → reaches `/dev/i2c-1` without root), from the venv
  where `oleddisplay` is installed.
- `--interval 5` = refresh seconds; `--rotate 3` = 270°. `--contrast 72` is the daytime
  brightness (0..255; the SH1107 default is 127, so dim below that). `--night-contrast 16`
  dims the panel during the night window **00:00–06:00** (set with `--night-start` /
  `--night-end`, Pi local time); it needs `--contrast` as the daytime value to restore to.
- Weather page: on by default. `--no-weather` disables it, `--page-seconds` sets the
  switch cadence (default 15), `--weather-refresh` the fetch period (default 1800 s). A
  fixed `--latitude`/`--longitude` (with optional `--city`) skips IP geolocation — this
  unit pins **Rijeka** (45.327, 14.442).
- Web panel: on by default on port **8080** (`--web-port`; `--no-web` disables it,
  `--config` overrides the `config.json` path). See the next section.
- The screen **blanks on stop/shutdown**: the app catches SIGTERM and clears the panel, so
  the last frame doesn't stay burned on until power-off.

Manage it:

```bash
sudo systemctl status oled-stats
sudo systemctl restart oled-stats
sudo journalctl -u oled-stats -f
```

## Web config panel

The service also serves a small config page — open it from any device on the same network:

```
http://192.168.10.109:8080      # the Pi's IP, port 8080
```

From it you can, live (changes show up on the next redraw, no restart):

- **System info** — tick which rows to show and reorder them with the ↑/↓ buttons (the top
  row is drawn first). The fan row is hidden automatically on a board with no fan.
- **Weather** — turn the weather page on or off entirely; set the city by name (it is
  geocoded to coordinates via Open-Meteo), or switch to **Auto (by IP)**.
- **Local meteo** — turn the indoor AHT20 + BMP280 sensor page on or off, and set a
  **temperature compensation** offset. The sensor sits close to the OLED panel and the board,
  so it reads a little high; the picker nudges the *displayed* room temperature by a fixed
  amount (range **−5…+5 °C**, step **0.5**, default **0**). The offset applies only to the
  shown temperature — humidity and pressure, and the raw value in the service log, are
  untouched.
- **Home Assistant** — turn MQTT publishing of the indoor readings on or off (see the next
  section). The broker address and credentials live in `config.json` on the device and are
  **not** editable from the panel — only this toggle is.

Edits are saved to `config.json` next to the app (git-ignored per-device state; the
`--latitude`/`--longitude`/`--city`/`--weather` flags seed it only on first run). A city
change is pushed straight into the running weather thread and refetched at once. The panel
is HTTP-only with no authentication — keep it on a trusted LAN.

## Home Assistant (MQTT)

The indoor AHT20 + BMP280 readings can be published to an MQTT broker so **Home Assistant**
picks them up as native sensors. It uses HA's **MQTT Discovery**: on connect the service
publishes a retained discovery config per measurement, so HA auto-creates *Temperature*,
*Humidity* and *Pressure* entities grouped under one device (**OLED meteo**) — no YAML on the
HA side. The live values then go as one retained JSON state message.

Publishing is **event-driven**: each reading is sent the instant it is taken (once per
`--meteo-refresh`, 30 s by default), reusing the same read the dashboard already does — so it
adds **no extra I²C traffic**. The published room temperature carries the same temperature
compensation offset as the screen; humidity and pressure are sent as measured. A Last-Will
message flips the device **offline** in HA if the service dies (or the toggle is turned off),
so a dead sensor is flagged rather than left showing a stale value.

Turn it on or off **live** from the web panel's **Home Assistant** checkbox. The broker
connection lives in the `mqtt` block of `config.json` on the device — never sent to the
browser (only the on/off toggle is):

```json
"mqtt": {
  "enabled": true,
  "host": "127.0.0.1",
  "port": 1883,
  "username": "mqtt",
  "password": "…"
}
```

Needs a running MQTT broker (e.g. Mosquitto) reachable at `host:port`; standing one up is out
of scope here. The client is `paho-mqtt` (in `requirements.txt`) — if it is missing the
publisher logs a warning and the dashboard runs unaffected.

## Cautions

- **Don't open the display from a second process** while the service runs: on exit `luma`
  sends display-off (`0xAE`) and the panel goes dark until the service is restarted — stop
  the service first (`sudo systemctl stop oled-stats`).
- The hardware quirk (spurious NAKs on the least-significant bit) and its workaround are
  documented in the library repo's README.
