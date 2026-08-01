#!/usr/bin/env python3
"""Publishes the indoor meteo readings to an MQTT broker for Home Assistant.

Bridges the local AHT20+BMP280 readings (:mod:`local_meteo`) onto MQTT so Home Assistant shows
them as native sensors. It uses HA's **MQTT Discovery**: on every (re)connect it publishes one
retained discovery config per measurement under ``<discovery_prefix>/sensor/…``, so HA
auto-creates a *Temperature*, *Humidity* and *Pressure* sensor grouped under one device — no YAML
on the HA side. The live values then go as one retained JSON state message that the three sensors
read through a value template.

Publishing is **event-driven, not timed**: :meth:`MqttPublisher.publish_reading` is wired as the
meteo service's ``on_reading`` hook, so exactly one state message goes out the moment each reading
is taken (every ``--meteo-refresh`` seconds) — always fresh, with no second timer to drift against.
paho-mqtt's own thread runs the network loop (connect/reconnect, callbacks).

The same hook honours the web panel's **live enable/disable toggle** (``mqtt.enabled`` in
config.json): while enabled the publisher lazily connects and publishes; when it is switched off
the publisher marks the device offline and disconnects — all without a service restart. A
Last-Will message also flips the device offline if the process dies, so HA flags a dead sensor
rather than showing a stale value.

The published room temperature carries the same ``meteo.temp_offset`` compensation as the OLED
page, read fresh from the shared config each reading. Broker host + credentials come from the
``mqtt`` block of config.json (see :class:`config.MqttConf`); the password is never sent to the
web panel — only the boolean toggle is.

Depends on ``paho-mqtt``. If the import fails the publisher degrades to a logged no-op rather than
taking the display process down.
"""

import json
from collections import namedtuple

try:
    import paho.mqtt.client as mqtt
except ImportError:   # keep the dashboard alive even if the dep is missing on an old venv
    mqtt = None

PAHO_AVAILABLE = mqtt is not None

# One HA device groups the three sensors. A stable node id keeps the entities' unique_ids constant
# across restarts, so HA re-attaches to the same entities instead of creating duplicates.
DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "oled_meteo"
STATE_TOPIC = f"{NODE_ID}/state"
AVAILABILITY_TOPIC = f"{NODE_ID}/availability"
ONLINE = "online"
OFFLINE = "offline"

# Each measurement HA should create: the JSON key in the state message, the entity name, and the
# HA metadata (device_class + unit) that gives it the right icon, unit and history graph.
Measurement = namedtuple("Measurement", "key name device_class unit")
MEASUREMENTS = [
    Measurement("temperature", "Temperature", "temperature", "°C"),
    Measurement("humidity", "Humidity", "humidity", "%"),
    Measurement("pressure", "Pressure", "pressure", "hPa"),
]


def _device_block(hostname):
    """The HA device the three sensors attach to (shown as one card in HA)."""
    return {
        "identifiers": [NODE_ID],
        "name": f"OLED meteo ({hostname})",
        "manufacturer": "DIY",
        "model": "AHT20 + BMP280",
    }


def _discovery_topic(measurement):
    """The retained config topic HA watches to auto-create ``measurement``'s sensor."""
    return f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{measurement.key}/config"


def _discovery_payload(measurement, device):
    """The HA MQTT-Discovery config for one sensor: where its value lives and how to show it."""
    return {
        "name": measurement.name,
        "unique_id": f"{NODE_ID}_{measurement.key}",
        "state_topic": STATE_TOPIC,
        "availability_topic": AVAILABILITY_TOPIC,
        "value_template": f"{{{{ value_json.{measurement.key} }}}}",
        "unit_of_measurement": measurement.unit,
        "device_class": measurement.device_class,
        "state_class": "measurement",
        "device": device,
    }


def _state_payload(reading, temp_offset):
    """Build the state dict from a :class:`local_meteo.MeteoReading`, applying the temp offset.

    Values are quantised to smooth sensor jitter: the room temperature carries the same
    ``temp_offset`` compensation as the OLED page and is then snapped to the nearest **0.5 °C**;
    humidity and pressure are published as whole units (0.1 hPa is below meaningful barometer
    accuracy — just noise here). A sensor that failed this cycle (its field is ``None``) is
    omitted, so HA keeps its last value instead of flipping to *unknown* on a momentary glitch.
    Returns ``{}`` when nothing is readable.
    """
    state = {}
    if reading.temp_c is not None:
        state["temperature"] = round((reading.temp_c + temp_offset) * 2) / 2   # nearest 0.5 °C
    if reading.humidity is not None:
        state["humidity"] = round(reading.humidity)          # whole percent
    if reading.pressure_hpa is not None:
        state["pressure"] = round(reading.pressure_hpa)      # whole hPa
    return state


class MqttPublisher:
    """Publishes each meteo reading to the broker, driven by the meteo service's ``on_reading``.

    Holds at most one paho client. :meth:`publish_reading` — the meteo hook, called once per read
    — reconciles the connection with the live ``mqtt.enabled`` toggle (connect + publish while on,
    disconnect + mark offline while off) and publishes the reading. The paho network thread
    (``loop_start``) owns reconnection; :meth:`_on_connect` (re)publishes the discovery configs and
    availability on every connect so HA re-discovers after either side restarts.
    """

    def __init__(self, *, config_store, hostname, log=print):
        self._config_store = config_store
        self._device = _device_block(hostname)
        self._log = log
        self._client = None
        self._last_reading = None

    def publish_reading(self, reading):
        """Meteo ``on_reading`` hook: reconcile the connection with the toggle, then publish.

        Runs on the meteo thread once per sensor read. Reading the toggle here (rather than only at
        startup) is what makes the web-panel checkbox take effect live: turning it off disconnects
        within one read, turning it on connects within one read.
        """
        self._last_reading = reading
        if self._config_store.snapshot().mqtt.enabled:
            self._ensure_connected()
            self._publish_state(reading)
        else:
            self._ensure_disconnected()

    def _ensure_connected(self):
        """Open the broker connection once; paho's loop then keeps it up. No-op if already up."""
        if self._client is not None:
            return
        if not PAHO_AVAILABLE:
            self._log("mqtt: paho-mqtt not installed — MQTT publishing unavailable")
            return
        conf = self._config_store.snapshot().mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if conf.username:
            client.username_pw_set(conf.username, conf.password)
        client.will_set(AVAILABILITY_TOPIC, OFFLINE, qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        self._client = client
        try:
            client.connect(conf.host, conf.port, keepalive=60)
        except OSError as error:
            self._log(f"mqtt: connect to {conf.host}:{conf.port} failed ({error}); paho will retry")
        client.loop_start()
        self._log(f"mqtt: connecting -> {conf.host}:{conf.port}")

    def _ensure_disconnected(self):
        """Mark the device offline and drop the connection. No-op if already disconnected."""
        if self._client is None:
            return
        client = self._client
        self._client = None
        try:
            client.publish(AVAILABILITY_TOPIC, OFFLINE, qos=1, retain=True)
            client.loop_stop()
            client.disconnect()
        except Exception as error:   # a teardown hiccup must not kill the meteo thread
            self._log(f"mqtt: error while disconnecting: {error}")
        self._log("mqtt: publishing disabled — disconnected")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """(Re)publish discovery + availability on each connect, then push the last reading.

        Doing this in the connect callback (not just once) means HA re-discovers the sensors and
        sees a fresh value after either the broker or this service restarts.
        """
        self._log(f"mqtt: connected (rc={reason_code})")
        for measurement in MEASUREMENTS:
            payload = json.dumps(_discovery_payload(measurement, self._device))
            client.publish(_discovery_topic(measurement), payload, qos=1, retain=True)
        client.publish(AVAILABILITY_TOPIC, ONLINE, qos=1, retain=True)
        if self._last_reading is not None:
            self._publish_state(self._last_reading)

    def _publish_state(self, reading):
        """Publish one retained JSON state message from ``reading`` (skips if nothing readable)."""
        if self._client is None:
            return
        temp_offset = self._config_store.snapshot().meteo.temp_offset
        state = _state_payload(reading, temp_offset)
        if not state:
            return
        self._client.publish(STATE_TOPIC, json.dumps(state), qos=1, retain=True)
        self._log(f"mqtt: published {state}")
