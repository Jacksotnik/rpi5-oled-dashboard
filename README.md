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
API key) for a location resolved automatically by IP, refreshed every 30 minutes on a
background thread.

> Roadmap: system metrics today; hardware sensors and their readouts next.

The panel is driven by the **`oleddisplay`** library, installed into the venv from its own
repository — no copy is kept here:
<https://github.com/Jacksotnik/rpi5-sh1107-oled-128x128>

## Contents

| Path | Purpose |
|------|---------|
| `stats_oled.py` | the app — metric collection, screen layout, refresh loop, page rotation |
| `weather.py` | weather data layer: IP geolocation + Open-Meteo fetch on a background thread |
| `requirements.txt` | venv deps: the `oleddisplay` library (from git) + `psutil` |
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
ExecStart=/home/admin/oled-stats/venv/bin/python /home/admin/oled-stats/stats_oled.py --interval 5 --rotate 3 --contrast 72 --night-contrast 16
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
  fixed `--latitude`/`--longitude` (with optional `--city`) skips IP geolocation.
- The screen **blanks on stop/shutdown**: the app catches SIGTERM and clears the panel, so
  the last frame doesn't stay burned on until power-off.

Manage it:

```bash
sudo systemctl status oled-stats
sudo systemctl restart oled-stats
sudo journalctl -u oled-stats -f
```

## Cautions

- **Don't open the display from a second process** while the service runs: on exit `luma`
  sends display-off (`0xAE`) and the panel goes dark until the service is restarted — stop
  the service first (`sudo systemctl stop oled-stats`).
- The hardware quirk (spurious NAKs on the least-significant bit) and its workaround are
  documented in the library repo's README.
