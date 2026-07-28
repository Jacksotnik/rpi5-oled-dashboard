# rpi5-oled-dashboard

System-stats service for a 1.5" OLED **128×128** panel (SH1107 driver, I²C) on a
Raspberry Pi 5. The screen shows a header (hostname + uptime) and six rows — CPU
(load / temperature), RAM (used / total), SSD (free / total + NVMe temperature),
SSID (with a WiFi signal-strength icon to the right of the network name), IP, and
Fan (rpm).

> Roadmap: the panel currently shows system metrics only; hardware sensors and
> their readouts are planned next.

The panel is driven by the **`oleddisplay` library, which is installed from a
separate repository** — there is no copy of its code here.

- Library repository: https://github.com/Jacksotnik/rpi5-sh1107-oled-128x128
- Local working copy (on the Mac): `~/my_projs/rpi5-sh1107-oled-128x128`

## Repository contents

| Path | Purpose |
|------|---------|
| `stats_oled.py` | the application: metric collection + screen layout + refresh loop; imports the installed `oleddisplay`. This is the canonical source of the app — edited here on the Mac and deployed to the Pi |
| `requirements.txt` | venv dependencies: the library from git + `psutil` |
| `oled-stats.service` | the systemd unit; deployed to `/etc/systemd/system/` on the Pi (see below) |
| `README.md` | this file |

The deploy layout on the Pi is `~/oled-stats/` — it holds `stats_oled.py`, the
`venv/` (where `oleddisplay` is installed) and `requirements.txt`. The systemd
unit lives in `/etc/systemd/system/oled-stats.service`, not in that directory.

## Architecture

- **Library `oleddisplay`** — installed into the `venv` from the GitHub repository
  (`pip install git+…`). The repository is its only source of code; there is no copy
  in this folder.
- **Application `stats_oled.py`** — a thin consumer of the library (RPi5 metrics +
  refresh loop). Its canonical source is **this repository**; the Pi runs a deployed
  copy.
- **Service `oled-stats.service`** — starts the application at boot and restarts it
  on failure.

## Autostart at boot (systemd)

The unit `/etc/systemd/system/oled-stats.service` (tracked here as `oled-stats.service`):

```ini
[Unit]
Description=OLED system stats display (SH1107 128x128, I2C)
After=multi-user.target

[Service]
Type=simple
User=admin
WorkingDirectory=/home/admin/oled-stats
ExecStart=/home/admin/oled-stats/venv/bin/python /home/admin/oled-stats/stats_oled.py --interval 5 --rotate 3 --contrast 72 --night-contrast 16
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Line by line:

- `After=multi-user.target` — starts after multi-user initialization (base services
  are already up).
- `Type=simple` — the process does not fork; systemd considers it started as soon as
  `ExecStart` runs.
- `User=admin` — runs as user `admin`, who is in the `i2c` group and therefore reaches
  `/dev/i2c-1` without root.
- `WorkingDirectory=/home/admin/oled-stats` — the process working directory.
- `ExecStart=…/venv/bin/python …/stats_oled.py --interval 5 --rotate 3 --contrast 72 --night-contrast 16`
  — launches the app with the interpreter **from the venv** (that is where `oleddisplay`
  is installed): refresh every 5 seconds, rotation `3` (270°), daytime contrast
  (brightness) `72` out of 0..255. The SH1107 default in luma is `127`, so for a
  noticeable dimming `--contrast` must be set well below 127. `--night-contrast 16`
  lowers brightness to `16` at night, **00:00–06:00** (the window is configurable via
  `--night-start` / `--night-end`, in the Pi's local time), to save power and slow OLED
  burn-in; it requires `--contrast` to be set (the value to restore to during the day).
- The screen **blanks on a normal stop/shutdown**: the app catches SIGTERM (sent by
  systemd on `stop`/`poweroff`) and clears the panel cleanly. Otherwise the last frame
  would stay "frozen" until power is removed from the module.
- `Restart=on-failure` + `RestartSec=3` — on a crash, restart after 3 seconds.
- `WantedBy=multi-user.target` — on `enable` a symlink is created in
  `multi-user.target.wants/`, so the service comes up on every boot.

The service is currently **enabled** (autostart on). Management:

```bash
sudo systemctl status oled-stats     # state
sudo systemctl restart oled-stats    # restart
sudo systemctl stop oled-stats       # stop
sudo systemctl start oled-stats      # start
sudo systemctl disable oled-stats    # disable autostart
sudo journalctl -u oled-stats -f     # follow logs live
```

## Build and deploy after changes

The library lives in its own repository; the application lives here. The steps depend
on what changed.

### A. Changes to the `oleddisplay` library

Edited in the library's working copy **on the Mac** (`~/my_projs/rpi5-sh1107-oled-128x128`).

1. Make the personal gh account active (required to push to the personal repo):
   ```bash
   gh auth switch --hostname github.com --user Jacksotnik
   ```
2. Edit the code and run the tests locally:
   ```bash
   cd ~/my_projs/rpi5-sh1107-oled-128x128
   ./.venv/bin/python -m unittest discover -s tests
   ```
3. Commit and push:
   ```bash
   git add -A && git commit -m "…"
   git push
   ```
4. **On the Pi**, reinstall the library from the fresh HEAD and restart the service:
   ```bash
   ssh rpi
   ~/oled-stats/venv/bin/pip install --upgrade --force-reinstall --no-deps --no-cache-dir \
       "git+https://github.com/Jacksotnik/rpi5-sh1107-oled-128x128.git"
   sudo systemctl restart oled-stats
   sudo journalctl -u oled-stats -n 20 --no-pager   # confirm no errors
   ```

> ⚠️ The package version usually does not change (`0.1.0`), so a plain
> `pip install -r requirements.txt` **will not** pull the new code — pip decides it is
> already installed. Updating requires the `--force-reinstall --no-cache-dir` flags.

### B. Changes to the `stats_oled.py` application

Edit `stats_oled.py` **here on the Mac**, commit/push, then deploy the file to the Pi
and restart the service:

1. Edit and do a quick syntax check locally:
   ```bash
   cd ~/my_projs/rpi5-oled-dashboard
   python3 -m py_compile stats_oled.py
   ```
2. Commit and push (personal account — see cautions):
   ```bash
   gh auth switch --hostname github.com --user Jacksotnik
   git add -A && git commit -m "…"
   git push
   ```
3. Deploy the file to the Pi and restart:
   ```bash
   scp stats_oled.py rpi:/home/admin/oled-stats/stats_oled.py
   ssh rpi 'sudo systemctl restart oled-stats && sudo journalctl -u oled-stats -n 20 --no-pager'
   ```

If the systemd unit itself changed (e.g. a different `--interval` or `--rotate`),
deploy `oled-stats.service` too:

```bash
scp oled-stats.service rpi:/tmp/oled-stats.service
ssh rpi 'sudo install -m 644 /tmp/oled-stats.service /etc/systemd/system/oled-stats.service \
    && sudo systemctl daemon-reload \
    && sudo systemctl restart oled-stats'
```

### Recreating the venv from scratch

```bash
cd ~/oled-stats
python3 -m venv --system-site-packages venv
./venv/bin/pip install -r requirements.txt
```

## Important cautions

- **Do not access the display from a second process** while the service is running: on
  exit `luma` sends the panel a display-off (`0xAE`), the screen goes dark, and the
  service does not turn it back on. For manual experiments, first
  `sudo systemctl stop oled-stats`.
- **Pushing to the personal repository** goes under the active gh account — this repo
  needs `Jacksotnik` (`gh auth switch --hostname github.com --user Jacksotnik`). Reading
  (`git fetch` / `clone`) works on a public repo without switching.
- The hardware quirk (spurious NAKs on the least-significant bit) and its software
  workaround are described in the library repository's own README.
