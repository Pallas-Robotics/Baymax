"""
Baymax Mobile Web App — Fall Detection Log + Voice Analysis
Runs a Flask server accessible from any device on the local network.
Receives fall events and transcript updates from the AI core (realtime_gemini_10.py)
via POST from this machine. Everything else needs a logged-in account that has
paired this robot. Emails fall and voice alerts via Gmail SMTP to those accounts.
"""
import json
import os
import smtplib
import threading
import time
import glob
import wave
import tempfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import uuid
import random
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from flask import Flask, Response, jsonify, request, send_from_directory, session, stream_with_context
from dotenv import load_dotenv

import device_config

load_dotenv()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Runtime data (logs, memory, accounts, audio). Defaults to next to the code;
# the container sets BAYMAX_DATA_DIR=/data, mounted from /var/lib/ember.
DATA_DIR = os.getenv("BAYMAX_DATA_DIR", SCRIPT_DIR)
FALL_LOG_PATH = os.path.join(DATA_DIR, "fall_log.json")
TRANSCRIPT_LOG_PATH = os.path.join(DATA_DIR, "transcript_log.json")
VOICE_RESULTS_PATH = os.path.join(DATA_DIR, "voice_analysis_results.json")
VOICE_ALERTS_PATH = os.path.join(DATA_DIR, "voice_alerts.json")
USERS_PATH = os.path.join(DATA_DIR, "users.json")
STATIC_DIR = os.path.join(SCRIPT_DIR, "static")

# ─── Known device registry ───────────────────────────────────────────────────
# This app serves exactly one robot: the one it runs on (device.toml).
DEVICE_SERIAL = device_config.get("device.serial")
KNOWN_DEVICES = {
    DEVICE_SERIAL: {"name": device_config.get("device.name"), "model": device_config.get("device.model")},
}

# The AI core's MJPEG server listens on loopback only; /api/stream relays it.
MJPEG_URL = "http://127.0.0.1:8080/stream"

# Alerts go to the registered email of every account paired to this robot
# (a fixed BAYMAX_EMAIL_TO address is no longer used).
EMAIL_FROM = os.getenv("BAYMAX_EMAIL_FROM", "")
EMAIL_PASSWORD = os.getenv("BAYMAX_EMAIL_PASSWORD", "")

app = Flask(__name__, static_folder=STATIC_DIR)

def _get_secret_key():
    key_path = os.path.join(DATA_DIR, ".flask_secret")
    if os.path.exists(key_path):
        with open(key_path) as f:
            return f.read().strip()
    import secrets as _sec
    key = _sec.token_hex(32)
    with open(key_path, "w") as f:
        f.write(key)
    return key

app.secret_key = os.getenv("FLASK_SECRET_KEY") or _get_secret_key()

_fall_log_lock = threading.Lock()
_transcript_lock = threading.Lock()
_voice_results_lock = threading.Lock()
_voice_alerts_lock = threading.Lock()
_users_lock = threading.Lock()
# email -> {username, password_hash, code, expires_at}
_pending_signups: dict = {}
_pending_lock = threading.Lock()

# ─── On-demand voice analysis state ─────────────────────────────────────────
_analysis_running = False
_analysis_lock = threading.Lock()
DAY_UTTERANCE_DIR = os.path.join(DATA_DIR, "day_utterance")

# ─── Boot status tracking ────────────────────────────────────────────────────
_boot_status = {
    "stage": "waiting",
    "message": "Waiting for Baymax to start...",
    "ready": False,
    "stages_completed": [],
    "stages_failed": {},
}
_boot_lock = threading.Lock()


def _load_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ─── User management ─────────────────────────────────────────────────────────

def _load_users():
    try:
        with open(USERS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def _save_users(users):
    with open(USERS_PATH, "w") as f:
        json.dump(users, f, indent=2)

def _safe_user(u):
    return {
        "id": u["id"],
        "username": u["username"],
        "email": u.get("email", ""),
        "devices": u.get("devices", []),
    }

def _current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with _users_lock:
        users = _load_users()
    return next((u for u in users if u["id"] == uid), None)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ─── Access control ──────────────────────────────────────────────────────────
# Every route needs a logged-in account, and anything showing this robot's data
# (falls, voice results, boot status, camera) needs that account to have paired
# it. The exceptions are the sign-in flow, and the AI core on this machine
# posting its events.

_PUBLIC_PATHS = {
    "/", "/care", "/api/auth/signup", "/api/auth/verify", "/api/auth/resend",
    "/api/auth/login", "/api/auth/logout", "/api/auth/me",
}
_ACCOUNT_PATHS = {"/api/devices", "/api/devices/pair"}  # login only, no pairing yet
_ROBOT_POSTS = {"/api/fall", "/api/transcript", "/api/boot-status", "/api/voice-analysis"}
_LOOPBACK = {"127.0.0.1", "::1"}


@app.before_request
def _require_access():
    path = request.path
    if path in _PUBLIC_PATHS or request.endpoint == "static":
        return None
    if request.method == "POST" and path in _ROBOT_POSTS and request.remote_addr in _LOOPBACK:
        return None
    user = _current_user()
    if user is None:
        return jsonify({"error": "unauthorized"}), 401
    if path not in _ACCOUNT_PATHS and DEVICE_SERIAL not in user.get("devices", []):
        return jsonify({"error": "this account has not paired this robot"}), 403
    return None


# ─── Auth routes ─────────────────────────────────────────────────────────────

def _send_verification_email(to_email: str, code: str):
    if not all([EMAIL_FROM, EMAIL_PASSWORD]):
        print(f"[AUTH] Email not configured — verification code for {to_email}: {code}")
        return False
    def _send():
        try:
            msg = MIMEMultipart()
            msg["From"] = EMAIL_FROM
            msg["To"] = to_email
            msg["Subject"] = "Baymax — Your verification code"
            body = (
                f"Your Baymax account verification code is:\n\n"
                f"  {code}\n\n"
                f"This code expires in 10 minutes.\n"
                f"If you did not request this, ignore this email."
            )
            msg.attach(MIMEText(body, "plain"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(EMAIL_FROM, EMAIL_PASSWORD)
                server.sendmail(EMAIL_FROM, to_email, msg.as_string())
            print(f"[AUTH] Verification email sent to {to_email}")
        except Exception as e:
            print(f"[AUTH] Failed to send verification email: {e}")
    threading.Thread(target=_send, daemon=True).start()
    return True


@app.route("/api/auth/signup", methods=["POST"])
def auth_signup():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")

    if not username or not email or not password:
        return jsonify({"error": "Username, email and password are required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    with _users_lock:
        users = _load_users()
        if any(u.get("username", "").lower() == username.lower() for u in users):
            return jsonify({"error": "Username already taken"}), 409
        if any(u.get("email", "") == email for u in users):
            return jsonify({"error": "Email already registered"}), 409

    code = f"{random.randint(0, 999999):06d}"
    expires_at = time.time() + 600  # 10 minutes

    with _pending_lock:
        _pending_signups[email] = {
            "username": username,
            "password_hash": generate_password_hash(password),
            "code": code,
            "expires_at": expires_at,
        }

    email_sent = _send_verification_email(email, code)
    if not email_sent:
        # Email not configured — include code in response for local dev
        return jsonify({"status": "verification_required", "dev_code": code}), 200

    return jsonify({"status": "verification_required"}), 200


@app.route("/api/auth/verify", methods=["POST"])
def auth_verify():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()

    with _pending_lock:
        pending = _pending_signups.get(email)

    if not pending:
        return jsonify({"error": "No pending signup for this email"}), 400
    if time.time() > pending["expires_at"]:
        with _pending_lock:
            _pending_signups.pop(email, None)
        return jsonify({"error": "Verification code expired — please sign up again"}), 400
    if code != pending["code"]:
        return jsonify({"error": "Incorrect code — check your email and try again"}), 401

    with _pending_lock:
        _pending_signups.pop(email, None)

    with _users_lock:
        users = _load_users()
        user = {
            "id": str(uuid.uuid4()),
            "username": pending["username"],
            "email": email,
            "password_hash": pending["password_hash"],
            "devices": [],
            "created_at": datetime.now().isoformat(),
        }
        users.append(user)
        _save_users(users)

    session["user_id"] = user["id"]
    return jsonify({"status": "ok", "user": _safe_user(user)}), 201


@app.route("/api/auth/resend", methods=["POST"])
def auth_resend():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    with _pending_lock:
        pending = _pending_signups.get(email)

    if not pending:
        return jsonify({"error": "No pending signup for this email"}), 400

    code = f"{random.randint(0, 999999):06d}"
    with _pending_lock:
        _pending_signups[email]["code"] = code
        _pending_signups[email]["expires_at"] = time.time() + 600

    _send_verification_email(email, code)
    return jsonify({"status": "ok"}), 200


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")

    if not username or not email or not password:
        return jsonify({"error": "Username, email and password are required"}), 400

    with _users_lock:
        users = _load_users()
    user = next(
        (u for u in users
         if u.get("username", "").lower() == username.lower()
         and u.get("email", "") == email),
        None,
    )
    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect username, email or password"}), 401

    session["user_id"] = user["id"]
    return jsonify({"status": "ok", "user": _safe_user(user)})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    user = _current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"user": _safe_user(user)})


# ─── Device routes ────────────────────────────────────────────────────────────

@app.route("/api/devices", methods=["GET"])
@login_required
def get_devices():
    user = _current_user()
    result = []
    for serial in user.get("devices", []):
        info = KNOWN_DEVICES.get(serial, {"name": f"Device {serial}", "model": "Unknown"})
        result.append({"serial": serial, **info})
    return jsonify(result)


@app.route("/api/devices/pair", methods=["POST"])
@login_required
def pair_device():
    data = request.get_json(silent=True) or {}
    serial = (data.get("serial_number") or "").strip()

    if not serial:
        return jsonify({"error": "serial number is required"}), 400
    if serial not in KNOWN_DEVICES:
        return jsonify({"error": "Device not found. Check the serial number on your Baymax unit."}), 404

    with _users_lock:
        users = _load_users()
        user = next((u for u in users if u["id"] == session["user_id"]), None)
        if not user:
            return jsonify({"error": "user not found"}), 404
        if serial in user.get("devices", []):
            return jsonify({"error": "device already paired"}), 409
        user.setdefault("devices", []).append(serial)
        _save_users(users)

    info = KNOWN_DEVICES[serial]
    return jsonify({"status": "ok", "device": {"serial": serial, **info}}), 201


def _alert_recipients():
    """@return registered emails of every account paired to this robot."""
    with _users_lock:
        users = _load_users()
    emails = []
    for u in users:
        email = (u.get("email") or "").strip()
        if DEVICE_SERIAL in u.get("devices", []) and email and email not in emails:
            emails.append(email)
    return emails


def _send_alert_email(subject, body, kind, recipients=None):
    """Emails each recipient separately, so paired accounts don't see each other's address.
    `recipients` defaults to every account paired to this robot."""
    if not (EMAIL_FROM and EMAIL_PASSWORD):
        print(f"[EMAIL] Email not configured — {kind} not sent.")
        return
    recipients = _alert_recipients() if recipients is None else recipients
    if not recipients:
        print(f"[EMAIL] No account has paired unit {DEVICE_SERIAL} — {kind} not sent.")
        return

    def _send():
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(EMAIL_FROM, EMAIL_PASSWORD)
                for to in recipients:
                    msg = MIMEMultipart()
                    msg["From"] = EMAIL_FROM
                    msg["To"] = to
                    msg["Subject"] = subject
                    msg.attach(MIMEText(body, "plain"))
                    server.sendmail(EMAIL_FROM, to, msg.as_string())
            print(f"[EMAIL] {kind} sent to {', '.join(recipients)}")
        except Exception as e:
            print(f"[EMAIL] Failed to send {kind}: {e}")

    threading.Thread(target=_send, daemon=True).start()


def _send_email_alert(fall_entry):
    body = (
        f"A fall was detected at {fall_entry['time']} on {fall_entry['date']}.\n\n"
        f"Trunk angle: {fall_entry.get('trunk_angle', 'N/A')}\n"
        f"Angular velocity: {fall_entry.get('angular_vel', 'N/A')}\n"
        f"Hip descent velocity: {fall_entry.get('hip_descent_vel', 'N/A')}\n\n"
        "Please check on the user immediately."
    )
    _send_alert_email("BAYMAX ALERT: Fall Detected", body, "fall alert")


@app.route("/api/stream")
def camera_stream():
    """The AI core's annotated camera feed, relayed so it sits behind the login."""
    try:
        upstream = requests.get(MJPEG_URL, stream=True, timeout=(2, 10))
    except requests.RequestException:
        return jsonify({"error": "camera stream unavailable"}), 503

    def relay():
        try:
            for chunk in upstream.iter_content(chunk_size=16384):
                yield chunk
        except requests.RequestException:
            pass  # AI core restarted or stopped sending frames
        finally:
            upstream.close()

    return Response(
        stream_with_context(relay()),
        content_type=upstream.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame"),
    )


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/boot-status", methods=["GET"])
def get_boot_status():
    with _boot_lock:
        return jsonify(_boot_status)


@app.route("/api/boot-status", methods=["POST"])
def post_boot_status():
    data = request.get_json(silent=True) or {}
    with _boot_lock:
        if "stage" in data:
            _boot_status["stage"] = data["stage"]
        if "message" in data:
            _boot_status["message"] = data["message"]
        if "ready" in data:
            _boot_status["ready"] = data["ready"]
        stage = data.get("stage")
        if stage:
            if data.get("error"):
                _boot_status["stages_failed"][stage] = data.get("message", "Error")
            else:
                if stage not in _boot_status["stages_completed"]:
                    _boot_status["stages_completed"].append(stage)
                _boot_status["stages_failed"].pop(stage, None)
    print(f"[BOOT] {data.get('message', '')}")
    return jsonify({"status": "ok"}), 200


@app.route("/api/falls", methods=["GET"])
def get_falls():
    with _fall_log_lock:
        falls = _load_json(FALL_LOG_PATH)
    return jsonify(falls)


@app.route("/api/fall", methods=["POST"])
def report_fall():
    data = request.get_json(silent=True) or {}
    now = datetime.now()
    entry = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timestamp": now.isoformat(),
        "trunk_angle": data.get("trunk_angle"),
        "angular_vel": data.get("angular_vel"),
        "hip_descent_vel": data.get("hip_descent_vel"),
    }

    with _fall_log_lock:
        falls = _load_json(FALL_LOG_PATH)
        falls.insert(0, entry)
        _save_json(FALL_LOG_PATH, falls)

    _send_email_alert(entry)
    print(f"[FALL] Logged fall at {entry['time']}")
    return jsonify({"status": "ok"}), 201


@app.route("/api/transcript", methods=["GET"])
def get_transcript():
    with _transcript_lock:
        entries = _load_json(TRANSCRIPT_LOG_PATH)
    limit = request.args.get("limit", 50, type=int)
    return jsonify(entries[:limit])


@app.route("/api/transcript", methods=["POST"])
def post_transcript():
    data = request.get_json(silent=True) or {}
    speaker = data.get("speaker", "unknown")
    text = data.get("text", "")
    if not text.strip():
        return jsonify({"status": "empty"}), 400

    now = datetime.now()
    entry = {
        "speaker": speaker,
        "text": text.strip(),
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(),
    }

    with _transcript_lock:
        entries = _load_json(TRANSCRIPT_LOG_PATH)
        entries.insert(0, entry)
        if len(entries) > 500:
            entries = entries[:500]
        _save_json(TRANSCRIPT_LOG_PATH, entries)

    return jsonify({"status": "ok"}), 201


@app.route("/api/voice-analysis", methods=["GET"])
def get_voice_analysis():
    with _voice_results_lock:
        results = _load_json(VOICE_RESULTS_PATH)
    limit = request.args.get("limit", len(results), type=int)
    return jsonify(results[-limit:] if limit < len(results) else results)


@app.route("/api/voice-analysis", methods=["POST"])
def post_voice_analysis():
    data = request.get_json(silent=True) or {}
    session = data.get("session", {})
    summary = data.get("summary", {})

    if session:
        with _voice_results_lock:
            results = _load_json(VOICE_RESULTS_PATH)
            results.append(session)
            _save_json(VOICE_RESULTS_PATH, results)

    alerts = summary.get("alerts", [])
    if alerts:
        with _voice_alerts_lock:
            existing = _load_json(VOICE_ALERTS_PATH)
            existing.extend(alerts)
            _save_json(VOICE_ALERTS_PATH, existing)
        _send_voice_alert_email(alerts)

    print(f"[VOICE] Session results saved. Alerts: {len(alerts)}")
    return jsonify({"status": "ok"}), 201


@app.route("/api/voice-alerts", methods=["GET"])
def get_voice_alerts():
    with _voice_alerts_lock:
        alerts = _load_json(VOICE_ALERTS_PATH)
    limit = request.args.get("limit", 50, type=int)
    return jsonify(alerts[-limit:] if limit < len(alerts) else alerts)


def _send_voice_alert_email(alerts):
    body = "Voice biomarker analysis has detected the following patterns:\n\n"
    for alert in alerts:
        body += f"Pattern: {alert['name']}\n"
        body += f"Matching indicators: {alert['matching_indicators']}/{alert['total_indicators']}\n"
        body += f"Severity: {alert.get('severity', 'warning')}\n"
        for detail in alert.get("details", []):
            body += f"  - {detail}\n"
        body += "\n"
    body += (
        "This is a screening observation, not a diagnosis. "
        "Please consult a healthcare professional for evaluation."
    )
    _send_alert_email("BAYMAX ALERT: Voice Biomarker Pattern Detected", body, "voice alert")


def _run_on_demand_analysis():
    global _analysis_running
    try:
        from voice_analyzer import VoiceAnalyzer

        # Collect completed segments — skip the most-recently-modified file
        # (it may still be open and being written by the recording thread)
        seg_files = sorted(glob.glob(os.path.join(DAY_UTTERANCE_DIR, "seg_*.wav")))
        if not seg_files:
            print("[VOICE-OD] No audio segments found.")
            return

        now = time.time()
        safe_files = [f for f in seg_files if (now - os.path.getmtime(f)) > 5]
        if not safe_files:
            print("[VOICE-OD] No completed segments available yet.")
            return

        # Merge safe segments into a temporary WAV
        merged_path = os.path.join(DATA_DIR, f"od_session_{int(now)}.wav")
        try:
            VoiceAnalyzer.concatenate_wavs_list(safe_files, merged_path)
        except AttributeError:
            # Fall back: merge manually if the helper doesn't accept a list
            with wave.open(merged_path, "wb") as out_wf:
                params_set = False
                for path in safe_files:
                    try:
                        with wave.open(path, "rb") as wf:
                            if not params_set:
                                out_wf.setparams(wf.getparams())
                                params_set = True
                            out_wf.writeframes(wf.readframes(wf.getnframes()))
                    except Exception as e:
                        print(f"[VOICE-OD] Skipping {path}: {e}")
            if not params_set:
                print("[VOICE-OD] Could not read any segment files.")
                return

        # Build transcript from saved log (user utterances only)
        with _transcript_lock:
            entries = _load_json(TRANSCRIPT_LOG_PATH)
        user_lines = [e["text"] for e in reversed(entries) if e.get("speaker") == "user"]
        transcript = " ".join(user_lines)

        analyzer = VoiceAnalyzer()
        results = analyzer.analyze_session(merged_path, transcript)
        summary = analyzer.save_session_results(results)

        with _voice_results_lock:
            existing = _load_json(VOICE_RESULTS_PATH)
            existing.append(results)
            _save_json(VOICE_RESULTS_PATH, existing)

        alerts = summary.get("alerts", [])
        if alerts:
            with _voice_alerts_lock:
                existing_alerts = _load_json(VOICE_ALERTS_PATH)
                existing_alerts.extend(alerts)
                _save_json(VOICE_ALERTS_PATH, existing_alerts)
            _send_voice_alert_email(alerts)

        print(f"[VOICE-OD] Analysis complete. Sessions: {summary.get('session_count')}, "
              f"Alerts: {len(alerts)}")
    except Exception as e:
        print(f"[VOICE-OD] Analysis failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            os.remove(merged_path)
        except Exception:
            pass
        with _analysis_lock:
            _analysis_running = False


@app.route("/api/analyze-voice", methods=["POST"])
def trigger_voice_analysis():
    global _analysis_running
    with _analysis_lock:
        if _analysis_running:
            return jsonify({"status": "running", "message": "Analysis already in progress"}), 409
        if not os.path.isdir(DAY_UTTERANCE_DIR) or not glob.glob(
            os.path.join(DAY_UTTERANCE_DIR, "seg_*.wav")
        ):
            return jsonify({"status": "error", "message": "No audio recordings found yet"}), 400
        _analysis_running = True

    threading.Thread(target=_run_on_demand_analysis, daemon=True).start()
    return jsonify({"status": "started"}), 202


@app.route("/api/analyze-voice/status", methods=["GET"])
def voice_analysis_status():
    with _analysis_lock:
        running = _analysis_running
    return jsonify({"running": running})


# ─── Facility care log (features/care) ───────────────────────────────────────
from features.care import dashboard as _care_dashboard  # noqa: E402

_care_dashboard.register(
    app, _current_user, _alert_recipients,
    lambda recipients, subject, body, kind: _send_alert_email(subject, body, kind, recipients),
    {"serial": DEVICE_SERIAL, **KNOWN_DEVICES[DEVICE_SERIAL]}, STATIC_DIR,
    start_digest=__name__ == "__main__" or os.getenv("EMBER_CARE_DIGEST_IN_PROCESS") == "1")


if __name__ == "__main__":
    os.makedirs(STATIC_DIR, exist_ok=True)
    print(f"[APP] Fall log: {FALL_LOG_PATH}")
    print(f"[APP] Voice analysis: {VOICE_RESULTS_PATH}")
    print(f"[APP] Email alerts: {'ENABLED (to accounts paired to unit ' + DEVICE_SERIAL + ')' if EMAIL_FROM and EMAIL_PASSWORD else 'DISABLED'}")
    app.run(host="0.0.0.0", port=5000, debug=False)
