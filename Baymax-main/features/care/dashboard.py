"""Staff and family view of the care log, served by baymax_app's dashboard (port 5000).

baymax_app already requires a signed-in account that has paired this robot for every
/api route. On top of that, staff are the accounts whose email is in EMBER_STAFF_EMAILS;
every other paired account is family and sees only what the resident agreed to share.
"""
import os
import threading
import time
from datetime import datetime

from flask import Blueprint, jsonify, request, send_from_directory

from . import store
from .digest import staff_digest

blueprint = Blueprint("care", __name__)
_hooks = {}


def staff_emails():
    return {e.strip().lower() for e in os.getenv("EMBER_STAFF_EMAILS", "").split(",") if e.strip()}


def role(user):
    return "staff" if (user.get("email") or "").strip().lower() in staff_emails() else "family"


def days_arg(default, maximum=366):
    try:
        return max(1, min(int(request.args.get("days", default)), maximum))
    except ValueError:
        return default


@blueprint.before_request
def _facility_mode_only():
    if not store.enabled():
        return jsonify({"error": "facility mode is off (EMBER_CARE_ENABLED=0)"}), 404
    return None


def _user():
    return _hooks["current_user"]() or {}


def _staff_only():
    user = _user()
    if role(user) != "staff":
        return None, (jsonify({"error": "staff only"}), 403)
    return user, None


@blueprint.route("/care")
def page():
    return send_from_directory(_hooks["static_dir"], "care.html")


@blueprint.route("/api/care/today")
def today():
    who = role(_user())
    start, end = store.day_bounds(clock=store.log.clock)
    result = {"role": who, "events": store.log.events(start, end, audience=who)}
    if who == "staff":
        result["exceptions"] = store.log.exceptions()
        result["metrics"] = store.log.metrics(start, end)
    return jsonify(result)


@blueprint.route("/api/care/events")
def events():
    who = role(_user())
    kind = request.args.get("kind")
    since = time.time() - days_arg(7) * 86400
    return jsonify({"role": who, "events": store.log.events(since, kinds=[kind] if kind else None, audience=who)})


@blueprint.route("/api/care/events/<int:event_id>/handled", methods=["POST"])
def handled(event_id):
    user, refused = _staff_only()
    if refused:
        return refused
    if not store.log.handle(event_id, user.get("username") or user.get("email")):
        return jsonify({"error": "no open item with that ID"}), 404
    return jsonify({"ok": True})


@blueprint.route("/api/care/metrics")
def metrics():
    _, refused = _staff_only()
    if refused:
        return refused
    return jsonify(store.log.metrics(time.time() - days_arg(60) * 86400))


@blueprint.route("/api/care/export")
def export():
    """Everything staff can see for a period, for the facility console or the pilot time study."""
    _, refused = _staff_only()
    if refused:
        return refused
    since = time.time() - days_arg(7) * 86400
    return jsonify({"device": _hooks["device"], "events": store.log.events(since, limit=5000),
                    "metrics": store.log.metrics(since)})


@blueprint.route("/api/care/consent", methods=["GET", "POST"])
def consent():
    if request.method == "GET":
        return jsonify({"family_kinds": store.log.family_kinds(), "consent": store.log.setting("family_consent"),
                        "kinds": store.KINDS})
    user, refused = _staff_only()
    if refused:
        return refused
    body = request.get_json(silent=True) or {}
    if body.get("confirmed_with_resident") is not True:
        return jsonify({"error": "record consent only after asking the resident (confirmed_with_resident: true)"}), 400
    try:
        recorded = store.log.set_family_kinds(body.get("kinds") or [], user.get("username") or user.get("email"))
    except (ValueError, TypeError):
        return jsonify({"error": "kinds must be a list of known kinds"}), 400
    return jsonify(recorded)


# ── Morning email to staff ────────────────────────────────────────────────────
def digest_due(now, sent_on, at):
    """True once per day, at or after `at` ("HH:MM", local time)."""
    hour, minute = (int(part) for part in at.split(":"))
    return sent_on != now.date().isoformat() and (now.hour, now.minute) >= (hour, minute)


def send_digest_if_due(now=None):
    at = os.getenv("EMBER_CARE_DIGEST_TIME", "").strip()
    if not (store.enabled() and at):
        return False
    now = now or datetime.now().astimezone()
    if not digest_due(now, store.log.setting("digest_sent_on"), at):
        return False
    recipients = [e for e in _hooks["paired_emails"]() if e.lower() in staff_emails()]
    if not recipients:
        print("[CARE] No staff account has paired this robot; morning digest not sent.", flush=True)
        return False
    subject, body = staff_digest(store.log, _hooks["device"], now.date())
    _hooks["send_email"](recipients, subject, body, "care digest")
    store.log.set_setting("digest_sent_on", now.date().isoformat())
    return True


def _digest_loop():
    while True:
        try:
            send_digest_if_due()
            store.log.prune()
        except Exception as error:
            print(f"[CARE] Digest check failed: {type(error).__name__}", flush=True)
        time.sleep(60)


def register(app, current_user, paired_emails, send_email, device, static_dir, start_digest=True):
    """Called once by baymax_app."""
    _hooks.update(current_user=current_user, paired_emails=paired_emails, send_email=send_email,
                  device=device, static_dir=static_dir)
    app.register_blueprint(blueprint)
    if start_digest and store.enabled():
        threading.Thread(target=_digest_loop, name="care-digest", daemon=True).start()
