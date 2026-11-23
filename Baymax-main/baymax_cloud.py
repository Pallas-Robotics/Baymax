"""
Baymax cloud client — store-and-forward telemetry uplink.

Replaces the fire-and-forget _notify_webapp() helper in realtime_gemini_9.py,
which POSTed to http://localhost:5000 with a 2-second timeout and silently
swallowed every failure. On a LAN that was fine; against a cloud endpoint a
brief WiFi drop would permanently lose a fall event.

Everything here queues to disk first and is flushed by a background worker, so
telemetry survives network outages and process restarts.

Configuration (.env on the robot):
    BAYMAX_CLOUD_URL   https://tadashirobotics.com
    BAYMAX_DEVICE_KEY  <serial>.<key>   from POST /api/admin/devices
"""
import json
import os
import threading
import time
import glob

import requests
from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Runtime data (logs, memory, accounts, audio). Defaults to next to the code;
# the container sets BAYMAX_DATA_DIR=/data, mounted from /var/lib/ember.
DATA_DIR = os.getenv("BAYMAX_DATA_DIR", SCRIPT_DIR)
QUEUE_DIR = os.path.join(DATA_DIR, "cloud_queue")

CLOUD_URL = os.getenv("BAYMAX_CLOUD_URL", "https://tadashirobotics.com").rstrip("/")
DEVICE_KEY = os.getenv("BAYMAX_DEVICE_KEY", "")

# The old local endpoints map onto the device-authenticated cloud routes, so
# call sites in realtime_gemini_9.py keep using their original paths.
ENDPOINT_MAP = {
    "/api/fall": "/api/device/fall",
    "/api/voice-analysis": "/api/device/voice-analysis",
    "/api/boot-status": "/api/device/boot-status",
}

# Transcripts stay on the robot. The cloud gets falls, voice-analysis results
# (numbers, no text) and boot status; transcripts go to the local dashboard.
# Facility mode (features/care, off unless EMBER_CARE_ENABLED=1) also sends
# staff-visible care events, which can carry a short quote of the resident.
ROBOT_ONLY = {"/api/transcript", "/api/device/transcript"}

# Boot/heartbeat messages describe a moment in time. Replaying a five-minute-old
# "connecting to Gemini" after reconnecting would just confuse the dashboard.
EPHEMERAL_ENDPOINTS = {"/api/device/boot-status"}
EPHEMERAL_MAX_AGE = 60

MAX_QUEUE_FILES = 5000       # ~ a week of transcripts; prevents unbounded disk use
FLUSH_INTERVAL = 5           # seconds between flush attempts when idle
BACKOFF_MAX = 300            # cap the retry backoff at 5 minutes
REQUEST_TIMEOUT = 10
HEARTBEAT_INTERVAL = 60

_seq_lock = threading.Lock()
_seq = 0
_worker_started = False
_worker_lock = threading.Lock()
_online = threading.Event()
_online.set()


def _next_filename() -> str:
    """Monotonic name so the queue drains in the order events happened."""
    global _seq
    with _seq_lock:
        _seq += 1
        return os.path.join(QUEUE_DIR, f"{time.time():.6f}_{_seq:06d}.json")


def _enqueue(endpoint: str, data: dict):
    os.makedirs(QUEUE_DIR, exist_ok=True)

    pending = glob.glob(os.path.join(QUEUE_DIR, "*.json"))
    if len(pending) >= MAX_QUEUE_FILES:
        # Drop the oldest rather than the newest — recent events matter more.
        for path in sorted(pending)[: len(pending) - MAX_QUEUE_FILES + 1]:
            try:
                os.remove(path)
            except OSError:
                pass
        print(f"[CLOUD] Queue full — dropped oldest entries (limit {MAX_QUEUE_FILES})")

    payload = {"endpoint": endpoint, "data": data, "queued_at": time.time()}
    path = _next_filename()

    # Write to a temp name then rename, so a crash mid-write cannot leave a
    # half-written file that the worker would choke on.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[CLOUD] Could not queue {endpoint}: {e}")


def _send(entry: dict) -> bool:
    """@return True if delivered or permanently undeliverable (drop it)."""
    endpoint = entry["endpoint"]

    if endpoint in ROBOT_ONLY:
        return True  # queued before transcripts became robot-only — discard

    if endpoint in EPHEMERAL_ENDPOINTS:
        if time.time() - entry.get("queued_at", 0) > EPHEMERAL_MAX_AGE:
            return True  # stale heartbeat — discard, do not retry

    try:
        res = requests.post(
            f"{CLOUD_URL}{endpoint}",
            json=entry["data"],
            headers={"Authorization": f"Bearer {DEVICE_KEY}"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return False  # network problem — keep for retry

    if res.status_code < 300:
        return True

    if res.status_code in (400, 401, 403, 404):
        # Bad credentials or a malformed payload will never succeed on retry.
        print(f"[CLOUD] Dropping {endpoint}: HTTP {res.status_code} {res.text[:200]}")
        return True

    return False  # 5xx / 429 — server-side, worth retrying


def _worker():
    backoff = FLUSH_INTERVAL

    while True:
        paths = sorted(glob.glob(os.path.join(QUEUE_DIR, "*.json")))
        if not paths:
            time.sleep(FLUSH_INTERVAL)
            backoff = FLUSH_INTERVAL
            continue

        delivered = 0
        for path in paths:
            try:
                with open(path) as f:
                    entry = json.load(f)
            except (OSError, json.JSONDecodeError):
                # Unreadable entry would block the queue forever.
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue

            if not _send(entry):
                break

            try:
                os.remove(path)
            except OSError:
                pass
            delivered += 1

        if delivered:
            if not _online.is_set():
                print(f"[CLOUD] Reconnected — flushed {delivered} queued events")
            _online.set()
            backoff = FLUSH_INTERVAL
        else:
            if _online.is_set():
                print("[CLOUD] Upload failing — buffering to disk")
            _online.clear()
            backoff = min(backoff * 2, BACKOFF_MAX)

        time.sleep(backoff)


def _heartbeat():
    """
    Keeps boot_status.updated_at fresh so the dashboard can tell a running unit
    from one that was unplugged. The cloud marks a device offline after 2
    minutes without a check-in.
    """
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        if _last_boot_state["ready"]:
            notify("/api/boot-status", dict(_last_boot_state["payload"]))


_last_boot_state = {"ready": False, "payload": {}}


def start():
    """Idempotent — safe to call from both the main script and the agent."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        if not DEVICE_KEY:
            print("[CLOUD] BAYMAX_DEVICE_KEY is not set — telemetry will not be uploaded.")
            return

        os.makedirs(QUEUE_DIR, exist_ok=True)
        threading.Thread(target=_worker, daemon=True).start()
        threading.Thread(target=_heartbeat, daemon=True).start()
        _worker_started = True
        print(f"[CLOUD] Uplink started → {CLOUD_URL}")


def notify(endpoint: str, data: dict):
    """
    Drop-in replacement for _notify_webapp(). Returns immediately; delivery
    happens on the worker thread.
    """
    if not DEVICE_KEY or endpoint in ROBOT_ONLY:
        return

    mapped = ENDPOINT_MAP.get(endpoint, endpoint)

    if mapped == "/api/device/boot-status":
        _last_boot_state["payload"] = data
        _last_boot_state["ready"] = bool(data.get("ready"))

    start()
    _enqueue(mapped, data)


def is_online() -> bool:
    return _online.is_set()


def pending_count() -> int:
    return len(glob.glob(os.path.join(QUEUE_DIR, "*.json")))
