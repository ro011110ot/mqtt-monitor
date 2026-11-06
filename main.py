import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import psutil
from paho.mqtt.enums import CallbackAPIVersion

SECRETS_FILE = Path(__file__).with_name("secrets.env")


def load_env_file(path):
    """Load KEY=VALUE lines into os.environ; real env variables take precedence."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file(SECRETS_FILE)

# MQTT configuration (from secrets.env or environment variables)
MQTT_BROKER = os.environ.get("MQTT_BROKER", "")
if not MQTT_BROKER:
    sys.exit(f"MQTT_BROKER is not set. Copy secrets.example.env to {SECRETS_FILE}.")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "Manjaro")
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "host/monitor")
MQTT_STATUS_TOPIC = f"{MQTT_TOPIC}/status"
INTERVAL = float(os.environ.get("MONITOR_INTERVAL", "5"))

stop_event = threading.Event()


def read_temp(temps, *sensors):
    """Return the first available reading of the given sensors, or None."""
    for name in sensors:
        if temps.get(name):
            return round(temps[name][0].current, 1)
    return None


def kb_per_s(current, previous, elapsed):
    """Rate in KB/s; clamped at 0 if the counter dropped (interface removed)."""
    return round(max(current - previous, 0) / 1024 / elapsed, 1)


def get_stats(prev_io, elapsed):
    """Collect stats; network rates are averaged over the elapsed interval."""
    io = psutil.net_io_counters()
    temps = psutil.sensors_temperatures()
    cpu_temp = read_temp(temps, "coretemp")

    stats = {
        "cpu": psutil.cpu_percent(percpu=True)[:4],
        "cpu_temp": cpu_temp,
        "ram": psutil.virtual_memory().percent,
        "ssd_temp": read_temp(temps, "nvme"),
        "gpu_temp": cpu_temp,  # Intel UHD: identical to CPU temp
        "gpu_mem": 0,  # Shared memory
        "net_down": kb_per_s(io.bytes_recv, prev_io.bytes_recv, elapsed),
        "net_up": kb_per_s(io.bytes_sent, prev_io.bytes_sent, elapsed),
    }
    return stats, io


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"Connection failed: {reason_code}", file=sys.stderr)
        return
    print(f"Connected to {MQTT_BROKER}:{MQTT_PORT}")
    client.publish(MQTT_STATUS_TOPIC, "online", retain=True)


def on_disconnect(client, userdata, flags, reason_code, properties):
    if not stop_event.is_set():
        print(f"Disconnected ({reason_code}), reconnecting...", file=sys.stderr)


def run():
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.will_set(MQTT_STATUS_TOPIC, "offline", retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    # connect_async + loop_start: keeps retrying if the broker is unreachable
    client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()

    # Stop cleanly on Ctrl+C and on SIGTERM (e.g. systemd stop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())

    print(f"Monitoring started on {MQTT_CLIENT_ID}...")

    # Prime the counters so the first measurement covers a full interval
    psutil.cpu_percent(percpu=True)
    prev_io = psutil.net_io_counters()
    prev_time = time.monotonic()

    while not stop_event.wait(INTERVAL):
        now = time.monotonic()
        try:
            stats, prev_io = get_stats(prev_io, now - prev_time)
            prev_time = now  # only advance together with prev_io
            if client.is_connected():
                client.publish(MQTT_TOPIC, json.dumps(stats))
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)

    print("Stopping...")
    if client.is_connected():
        client.publish(MQTT_STATUS_TOPIC, "offline", retain=True).wait_for_publish(2)
    client.disconnect()
    client.loop_stop()


if __name__ == "__main__":
    run()
