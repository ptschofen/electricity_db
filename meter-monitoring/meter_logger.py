#!/usr/bin/env python3
"""
Subscribes to MQTT sensor readings published by ESPHome smart meter devices
(one per house) and logs every reading into a local SQLite database.

Expects ESPHome's default MQTT topic structure:
    <device_name>/sensor/<sensor_id>/state

e.g. "smartmeter-parents/sensor/active_power_plus/state" -> payload "1234.5"

Run this continuously (see the accompanying systemd service file) on the
same Raspberry Pi that runs the Mosquitto MQTT broker.
"""

import sqlite3
import sys
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# --- Config ---
MQTT_HOST = "localhost"   # broker runs on this same Pi
MQTT_PORT = 1883
MQTT_USER = "meterlogger"          # set to match your mosquitto password file
MQTT_PASSWORD = "CHANGE_ME"        # set to match your mosquitto password file
DB_PATH = "/home/pi/meter_readings.db"

# Maps each ESPHome device's node name to a short site label.
# Add an entry here for each house's ESP32.
DEVICE_TO_SITE = {
    "smartmeter-home": "home",
    "smartmeter-parents": "parents",
}


def parse_topic(topic: str):
    """
    Parse an ESPHome default-style MQTT topic into (site, sensor_id).
    Returns None if the topic doesn't match the expected pattern or
    comes from a device we don't recognize.
    """
    parts = topic.split("/")
    if len(parts) != 4:
        return None
    device_name, component, sensor_id, suffix = parts
    if component != "sensor" or suffix != "state":
        return None
    site = DEVICE_TO_SITE.get(device_name)
    if site is None:
        return None
    return site, sensor_id


def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meter_readings (
            site TEXT NOT NULL,
            sensor TEXT NOT NULL,
            ts_utc TEXT NOT NULL,
            value REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_meter_readings_site_sensor_ts
        ON meter_readings (site, sensor, ts_utc)
    """)
    conn.commit()
    return conn


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        print("Connected to MQTT broker.")
        client.subscribe("+/sensor/+/state")
        print("Subscribed to +/sensor/+/state")
    else:
        print(f"Connection failed with reason code: {reason_code}")


def on_message(client, userdata, msg):
    parsed = parse_topic(msg.topic)
    if parsed is None:
        return  # not a topic we care about (unknown device or wrong shape)
    site, sensor_id = parsed

    try:
        value = float(msg.payload.decode("utf-8"))
    except ValueError:
        print(f"Ignoring non-numeric payload on {msg.topic}: {msg.payload!r}")
        return

    ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = userdata["conn"]
    conn.execute(
        "INSERT INTO meter_readings (site, sensor, ts_utc, value) VALUES (?, ?, ?, ?)",
        (site, sensor_id, ts_utc, value),
    )
    conn.commit()
    print(f"[{ts_utc}] {site}/{sensor_id} = {value}")


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    print(f"Disconnected from broker (reason code: {reason_code}). paho will auto-reconnect.")


def main():
    conn = init_db(DB_PATH)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata={"conn": conn})
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    # Automatic reconnection with backoff if the broker restarts or the
    # network blips -- this is what keeps the logger genuinely "always on".
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    print(f"Connecting to {MQTT_HOST}:{MQTT_PORT} ...")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
