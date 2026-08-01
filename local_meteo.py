#!/usr/bin/env python3
"""Local weather sensor for the OLED meteo page.

Reads temperature + humidity from an **AHT20** and barometric pressure from a **BMP280**
(a common combined breakout) over the same I²C bus as the display. A background thread keeps
one cached :class:`MeteoReading` fresh on a slow timer, so the display loop never blocks on the
~80 ms AHT20 measurement — it just reads the latest snapshot via :meth:`MeteoService.latest`
(``None`` until the first read).

Only ``smbus2`` is used (already a dependency of the display library), talking to the chips by
raw register access — no heavyweight sensor stack. Both sensors sit on ``/dev/i2c-1`` at their
fixed addresses next to the SH1107 panel (``0x3c``); the three addresses don't collide.
"""

import json
import os
import threading
import time
from collections import namedtuple
from pathlib import Path

from smbus2 import SMBus, i2c_msg

# I²C addresses of the two chips on the breakout.
AHT20_ADDRESS = 0x38
BMP280_ADDRESS = 0x77

# --- AHT20 (temperature + humidity) --------------------------------------------------------
AHT20_CMD_INIT = 0xBE           # calibrate/init; followed by data bytes 0x08, 0x00
AHT20_CMD_MEASURE = 0xAC        # trigger a measurement; followed by data bytes 0x33, 0x00
AHT20_STATUS_BUSY = 0x80        # status bit 7: a measurement is still in progress
AHT20_STATUS_CALIBRATED = 0x08  # status bit 3: factory calibration is loaded
AHT20_MEASURE_SECONDS = 0.08    # datasheet: wait ≥ 75 ms after a measure command

# --- BMP280 (pressure) ---------------------------------------------------------------------
BMP280_REG_CHIP_ID = 0xD0
BMP280_CHIP_ID = 0x58           # distinguishes a real BMP280 from a BME280 (0x60)
BMP280_REG_CALIB = 0x88         # first of 24 calibration bytes (dig_T1..dig_P9)
BMP280_REG_CTRL_MEAS = 0xF4
BMP280_REG_DATA = 0xF7          # 6 data bytes: pressure[3] then temperature[3]
# ctrl_meas = osrs_t ×1 (001) | osrs_p ×4 (011) | forced mode (01) → 0b00101101
BMP280_CTRL_FORCED = 0x2D
# Worst-case conversion time at the ×1/×4 oversampling above is ~13 ms (datasheet §9.1). We wait
# out a safe budget rather than poll the status bit: right after triggering forced mode the
# "measuring" bit isn't set yet, so a poll would race and read the reset-default registers on the
# very first measurement.
BMP280_MEASURE_SECONDS = 0.02

# temp_c / humidity / pressure come from AHT20+BMP280; bmp_temp_c is the BMP280's own reading
# (kept for diagnostics — it self-heats a little). ``error`` is "" on a fully successful read.
MeteoReading = namedtuple(
    "MeteoReading", "temp_c humidity pressure_hpa bmp_temp_c updated error"
)


def _u16le(data, index):
    """Read an unsigned little-endian 16-bit value at ``index`` of a byte list."""
    return data[index] | (data[index + 1] << 8)


def _s16le(data, index):
    """Read a signed little-endian 16-bit value at ``index`` of a byte list."""
    value = _u16le(data, index)
    return value - 0x10000 if value >= 0x8000 else value


def _crc8(data):
    """AHT20 CRC-8 (polynomial 0x31, init 0xFF) over ``data`` — used to reject a garbled read."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x31) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def _fmt(value, digits=1):
    """Format a possibly-``None`` number for a log line."""
    return f"{value:.{digits}f}" if value is not None else "--"


class Aht20:
    """Reads temperature (°C) and relative humidity (%) from an AHT20."""

    def __init__(self, bus, address=AHT20_ADDRESS):
        self._bus = bus
        self._address = address
        self._ensure_calibrated()

    def _status(self):
        return self._bus.read_byte(self._address)

    def _ensure_calibrated(self):
        # A freshly powered chip may need its factory calibration loaded before it reads sanely.
        if self._status() & AHT20_STATUS_CALIBRATED:
            return
        self._bus.write_i2c_block_data(self._address, AHT20_CMD_INIT, [0x08, 0x00])
        time.sleep(0.01)

    def read(self):
        """Trigger a measurement and return ``(temp_c, humidity)`` (raises on an I²C/CRC error)."""
        self._bus.write_i2c_block_data(self._address, AHT20_CMD_MEASURE, [0x33, 0x00])
        time.sleep(AHT20_MEASURE_SECONDS)

        message = i2c_msg.read(self._address, 7)
        self._bus.i2c_rdwr(message)
        data = list(message)

        if data[0] & AHT20_STATUS_BUSY:
            raise TimeoutError("AHT20 still busy after the measurement wait")
        if _crc8(data[:6]) != data[6]:
            raise ValueError("AHT20 CRC mismatch")

        humidity_raw = (data[1] << 12) | (data[2] << 4) | (data[3] >> 4)
        temp_raw = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]
        humidity = humidity_raw * 100.0 / 0x100000     # raw / 2**20 * 100
        temp_c = temp_raw * 200.0 / 0x100000 - 50.0     # raw / 2**20 * 200 - 50
        return temp_c, humidity


class Bmp280:
    """Reads temperature (°C) and barometric pressure (hPa) from a BMP280.

    Reads the factory calibration coefficients once at construction; each :meth:`read` then takes
    a single forced-mode measurement and applies the datasheet integer compensation formulas.
    """

    def __init__(self, bus, address=BMP280_ADDRESS):
        self._bus = bus
        self._address = address
        chip_id = self._bus.read_byte_data(self._address, BMP280_REG_CHIP_ID)
        if chip_id != BMP280_CHIP_ID:
            raise ValueError(f"unexpected BMP280 chip id 0x{chip_id:02x} (expected 0x58)")
        self._load_calibration()

    def _load_calibration(self):
        data = self._bus.read_i2c_block_data(self._address, BMP280_REG_CALIB, 24)
        self._dig_t1 = _u16le(data, 0)
        self._dig_t2 = _s16le(data, 2)
        self._dig_t3 = _s16le(data, 4)
        self._dig_p1 = _u16le(data, 6)
        self._dig_p2 = _s16le(data, 8)
        self._dig_p3 = _s16le(data, 10)
        self._dig_p4 = _s16le(data, 12)
        self._dig_p5 = _s16le(data, 14)
        self._dig_p6 = _s16le(data, 16)
        self._dig_p7 = _s16le(data, 18)
        self._dig_p8 = _s16le(data, 20)
        self._dig_p9 = _s16le(data, 22)

    def read(self):
        """Take one forced-mode measurement → ``(temp_c, pressure_hpa)`` (raises on error)."""
        adc_temp, adc_press = self._measure_raw()
        temp_c, t_fine = self._compensate_temperature(adc_temp)
        pressure_hpa = self._compensate_pressure(adc_press, t_fine)
        return temp_c, pressure_hpa

    def _measure_raw(self):
        self._bus.write_byte_data(self._address, BMP280_REG_CTRL_MEAS, BMP280_CTRL_FORCED)
        time.sleep(BMP280_MEASURE_SECONDS)   # let the forced-mode conversion finish
        data = self._bus.read_i2c_block_data(self._address, BMP280_REG_DATA, 6)
        adc_press = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
        adc_temp = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
        return adc_temp, adc_press

    def _compensate_temperature(self, adc_temp):
        # Integer compensation from the BMP280 datasheet (§3.11.3). ``t_fine`` carries the fine
        # temperature into the pressure formula below.
        var1 = (((adc_temp >> 3) - (self._dig_t1 << 1)) * self._dig_t2) >> 11
        delta = (adc_temp >> 4) - self._dig_t1
        var2 = (((delta * delta) >> 12) * self._dig_t3) >> 14
        t_fine = var1 + var2
        temp_c = ((t_fine * 5 + 128) >> 8) / 100.0
        return temp_c, t_fine

    def _compensate_pressure(self, adc_press, t_fine):
        # 64-bit integer compensation from the BMP280 datasheet (§3.11.3); the result is Q24.8 Pa.
        var1 = t_fine - 128000
        var2 = var1 * var1 * self._dig_p6
        var2 = var2 + ((var1 * self._dig_p5) << 17)
        var2 = var2 + (self._dig_p4 << 35)
        var1 = ((var1 * var1 * self._dig_p3) >> 8) + ((var1 * self._dig_p2) << 12)
        var1 = (((1 << 47) + var1) * self._dig_p1) >> 33
        if var1 == 0:
            raise ZeroDivisionError("BMP280 pressure compensation hit a zero divisor")
        pressure = 1048576 - adc_press
        pressure = (((pressure << 31) - var2) * 3125) // var1
        var1 = (self._dig_p9 * (pressure >> 13) * (pressure >> 13)) >> 25
        var2 = (self._dig_p8 * pressure) >> 19
        pressure = ((pressure + var1 + var2) >> 8) + (self._dig_p7 << 4)
        return pressure / 256.0 / 100.0    # Q24.8 Pa → Pa → hPa


class MeteoSensor:
    """Reads both chips on the breakout and returns one combined :class:`MeteoReading`.

    Each sensor is read under its own guard: a failure of one (or its absence) leaves that
    sensor's fields ``None`` and is recorded in ``error`` rather than sinking the whole reading.
    A failed reader is dropped so the next cycle re-initialises it, letting a boot-time race or a
    momentary unplug self-heal.
    """

    def __init__(self, bus, *, log=print):
        self._bus = bus
        self._log = log
        self._aht20 = None
        self._bmp280 = None

    def _aht(self):
        if self._aht20 is None:
            self._aht20 = Aht20(self._bus)
        return self._aht20

    def _bmp(self):
        if self._bmp280 is None:
            self._bmp280 = Bmp280(self._bus)
        return self._bmp280

    def read(self):
        """Read both sensors and return a :class:`MeteoReading` (never raises for a sensor error)."""
        temp_c = None
        humidity = None
        pressure_hpa = None
        bmp_temp_c = None
        errors = []

        try:
            temp_c, humidity = self._aht().read()
        except Exception as error:   # I²C / CRC / absent chip — keep the other sensor alive
            self._aht20 = None       # drop it so the next cycle re-inits from scratch
            message = error.args[0] if error.args else str(error)
            errors.append(f"AHT20: {message}")
            self._log(f"meteo: AHT20 read failed: {message}")

        try:
            bmp_temp_c, pressure_hpa = self._bmp().read()
        except Exception as error:
            self._bmp280 = None
            message = error.args[0] if error.args else str(error)
            errors.append(f"BMP280: {message}")
            self._log(f"meteo: BMP280 read failed: {message}")

        return MeteoReading(
            temp_c=temp_c,
            humidity=humidity,
            pressure_hpa=pressure_hpa,
            bmp_temp_c=bmp_temp_c,
            updated=time.time(),
            error="; ".join(errors),
        )


# --- Pressure history (file-backed 12 h trend) ---------------------------------------------
DEFAULT_HISTORY_WINDOW_SECONDS = 12 * 3600   # keep the last 12 hours
DEFAULT_HISTORY_SAMPLE_SECONDS = 300         # record at most one sample every 5 minutes


def _as_list(value):
    """Return ``value`` if it is a list, else an empty list (defensive JSON read)."""
    return value if isinstance(value, list) else []


def _as_history_point(item):
    """Coerce one loosely-typed history entry to ``[epoch:int, hPa:float]`` or ``None``."""
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        return None
    try:
        return [int(item[0]), float(item[1])]
    except (TypeError, ValueError):
        return None


class PressureHistory:
    """A rolling, file-backed history of barometric pressure for the trend graph.

    Keeps one ``[epoch, hPa]`` sample at most every ``sample_seconds`` over the last
    ``window_seconds``, persisted to a JSON file so a reboot doesn't lose the recent trend.
    Thread-safe: the meteo service records from its own thread while the display loop reads the
    series to draw it. A read/write failure is logged, not raised — the trend is best-effort and
    must never take the readings down.
    """

    def __init__(self, path, *, window_seconds=DEFAULT_HISTORY_WINDOW_SECONDS,
                 sample_seconds=DEFAULT_HISTORY_SAMPLE_SECONDS, log=print):
        self._path = Path(path)
        self._window_seconds = window_seconds
        self._sample_seconds = sample_seconds
        self._log = log
        self._lock = threading.Lock()
        self._points = self._load()   # list of [epoch, hPa], oldest first

    def points(self):
        """Return a copy of the retained ``[epoch, hPa]`` samples (oldest first)."""
        with self._lock:
            return list(self._points)

    def record(self, pressure, now):
        """Append ``pressure`` (hPa) sampled at ``now`` if due, prune old samples and persist.

        Self-limits to one sample per ``sample_seconds``, so it is safe to call on every sensor
        read. A ``None`` pressure (sensor failure) is skipped.
        """
        if pressure is None:
            return
        with self._lock:
            if self._points and now - self._points[-1][0] < self._sample_seconds:
                return
            self._points.append([int(now), float(pressure)])
            self._prune(now)
            snapshot = list(self._points)
        self._write(snapshot)

    def _prune(self, now):
        cutoff = now - self._window_seconds
        self._points = [point for point in self._points if point[0] >= cutoff]

    def _load(self):
        """Read the history file, keeping only well-formed samples within the window."""
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError) as error:
            self._log(f"meteo: could not read pressure history ({error}); starting empty")
            return []
        cutoff = time.time() - self._window_seconds
        raw_points = _as_list(raw.get("points")) if isinstance(raw, dict) else []
        points = []
        for item in raw_points:
            point = _as_history_point(item)
            if point is not None and point[0] >= cutoff:
                points.append(point)
        points.sort(key=lambda point: point[0])
        return points

    def _write(self, points):
        """Atomically persist ``points`` (temp file in the same dir + replace)."""
        try:
            payload = json.dumps({"points": points})
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(payload)
            os.replace(tmp, self._path)
        except OSError as error:
            self._log(f"meteo: could not write pressure history: {error}")


class MeteoService:
    """Keeps one cached :class:`MeteoReading` fresh on a background thread.

    Opens its own handle on the I²C bus (the kernel serialises transactions, so sharing the bus
    with the display is safe) and re-reads both sensors every ``refresh_seconds``. The display
    loop calls :meth:`latest` (cheap, lock-guarded) and never blocks on a measurement; ``latest``
    is ``None`` until the first read. When ``history_path`` is given, each reading's pressure is
    also fed to a file-backed :class:`PressureHistory` for the 12 h trend graph.
    """

    def __init__(self, *, refresh_seconds, bus_number=1, history_path=None,
                 history_window_seconds=DEFAULT_HISTORY_WINDOW_SECONDS,
                 history_sample_seconds=DEFAULT_HISTORY_SAMPLE_SECONDS, log=print):
        self._refresh_seconds = refresh_seconds
        self._bus_number = bus_number
        self._log = log
        self._lock = threading.Lock()
        self._data = None
        self._sensor = None
        self._history = None
        if history_path is not None:
            self._history = PressureHistory(
                history_path, window_seconds=history_window_seconds,
                sample_seconds=history_sample_seconds, log=log)

    def latest(self):
        """Return the most recent :class:`MeteoReading`, or ``None`` if none yet."""
        with self._lock:
            return self._data

    def pressure_history(self):
        """Return the retained pressure samples (``[epoch, hPa]``, oldest first) for the graph."""
        if self._history is None:
            return []
        return self._history.points()

    def _store(self, data):
        with self._lock:
            self._data = data

    def _refresh_once(self):
        if self._sensor is None:
            self._sensor = MeteoSensor(SMBus(self._bus_number), log=self._log)
        reading = self._sensor.read()
        self._store(reading)
        if self._history is not None:
            self._history.record(reading.pressure_hpa, reading.updated)
        suffix = f" [{reading.error}]" if reading.error else ""
        self._log(
            f"meteo: temp={_fmt(reading.temp_c)}°C hum={_fmt(reading.humidity)}% "
            f"press={_fmt(reading.pressure_hpa)}hPa{suffix}"
        )

    def _run(self):
        # A plain service loop: read, then wait. One failed cycle must not kill the thread, so
        # the read is guarded and merely logged.
        while True:
            try:
                self._refresh_once()
            except Exception as error:   # bus open / anything unexpected — stay alive
                self._log(f"meteo: refresh failed: {error}")
            time.sleep(self._refresh_seconds)

    def start(self):
        """Launch the refresh loop on a daemon thread and return it."""
        thread = threading.Thread(target=self._run, name="meteo", daemon=True)
        thread.start()
        return thread


if __name__ == "__main__":
    # Smoke test: read both sensors a few times straight off the bus and print the readings.
    sensor = MeteoSensor(SMBus(1))
    for _ in range(3):
        print(sensor.read())
        time.sleep(1)
