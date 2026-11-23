"""The resident's care log: timestamped events that staff (and, with consent, family) see.

One robot serves one resident's apartment. Features record what happened (a check-in,
a request, a fall). The robot keeps the full log in local SQLite, and forwards what staff
may see to the facility page on tadashirobotics.com through baymax_cloud's store-and-
forward uplink (only when the unit has a device key). Ember stays non-medical: no
medication, diagnoses or clinical readings.
"""
import json
import os
import sqlite3
import statistics
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

# What features may record. Anything else is refused, so the log stays non-clinical.
KINDS = {
    "check_in": "Daily check-in result",
    "request": "Non-urgent request for staff (towels, a broken light, housekeeping)",
    "help_request": "The resident asked for a person to come",
    "night_wake": "Activity during the night",
    "fall": "Fall or long lie detected",
    "activity": "Took part in, skipped or asked about an activity",
    "meal": "Meal pre-order or menu feedback",
    "note": "Something staff should know that fits no other kind",
    "flag": "A pattern for staff to review (Ember suggests; staff decide)",
}
# open: staff should act. attention: staff should look. no_answer: expected reply missing.
STATUSES = ("info", "ok", "attention", "no_answer", "open", "handled")
EXCEPTIONS = ("attention", "no_answer", "open")
# Staff always see these, even when the resident asked to keep something private:
# safety comes first, and staff can't act on a hidden fall or unanswered check-in.
SAFETY_KINDS = ("fall", "help_request", "check_in")


def enabled():
    return os.getenv("EMBER_CARE_ENABLED", "0") == "1"


def text(value, maximum, name):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be nonempty text of at most {maximum} characters")
    return value.strip()


def iso(value):
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def day_bounds(day=None, clock=time.time):
    """(start, end) epoch seconds of a local calendar day (default today)."""
    day = day or datetime.fromtimestamp(clock()).date()
    start = datetime.combine(day, datetime.min.time()).astimezone()
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


class CareLog:
    def __init__(self, path=None, clock=time.time):
        root = Path(os.getenv("BAYMAX_DATA_DIR", Path(__file__).resolve().parents[2]))
        self.path = Path(path) if path else root / "care_log.sqlite3"
        self.clock = clock

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, "
                "kind TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL, quote TEXT, category TEXT, "
                "private INTEGER NOT NULL DEFAULT 0, data TEXT, handled_by TEXT, handled_at REAL)")
            connection.execute("CREATE INDEX IF NOT EXISTS events_at ON events(at)")
            connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ── Writing ──────────────────────────────────────────────────────────────
    def record(self, kind, summary, status="info", quote=None, category=None, private=False, data=None):
        """Add one event; returns its ID. `quote` is the resident's own words, when there are any."""
        if kind not in KINDS:
            raise ValueError(f"Unknown care event kind {kind!r}")
        if status not in STATUSES or status == "handled":
            raise ValueError(f"Unknown status {status!r}")
        summary = text(summary, 300, "summary")
        quote = text(quote, 500, "quote") if quote is not None else None
        category = text(category, 40, "category") if category is not None else None
        payload = json.dumps(data, ensure_ascii=False) if data is not None else None
        if payload is not None and (not isinstance(data, dict) or len(payload) > 4000):
            raise ValueError("data must be a JSON object under 4000 characters")
        with self.db() as db:
            event_id = db.execute(
                "INSERT INTO events (at, kind, status, summary, quote, category, private, data) VALUES (?,?,?,?,?,?,?,?)",
                (self.clock(), kind, status, summary, quote, category, int(bool(private)), payload)).lastrowid
            row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        forward("/api/device/care-event", self.row(row, "staff"), staff_visible=not private or kind in SAFETY_KINDS)
        return event_id

    def handle(self, event_id, by):
        """Staff mark an open/attention event as dealt with. Returns False if there was nothing to handle."""
        by = text(by, 120, "by")
        with self.db() as db:
            changed = db.execute(
                "UPDATE events SET status='handled', handled_by=?, handled_at=? WHERE id=? AND status IN (?,?,?)",
                (by, self.clock(), int(event_id), *EXCEPTIONS)).rowcount
        if changed:
            forward("/api/device/care-handled", {"id": int(event_id), "handled_by": by, "handled_at": iso(self.clock())})
        return changed == 1

    def keep_private(self, event_id):
        """The resident asked to keep this between them and Ember: hidden from family, and from
        staff unless it is a safety event."""
        with self.db() as db:
            row = db.execute("SELECT kind FROM events WHERE id=?", (int(event_id),)).fetchone()
            changed = db.execute("UPDATE events SET private=1 WHERE id=?", (int(event_id),)).rowcount == 1
        if changed:
            # The site deletes a non-safety event and drops a safety event's quote.
            forward("/api/device/care-private", {"id": int(event_id), "safety": row["kind"] in SAFETY_KINDS})
        return changed

    def prune(self, days=None):
        days = float(os.getenv("EMBER_CARE_RETENTION_DAYS", "90") if days is None else days)
        if days <= 0:
            return 0  # Same convention as audio retention: 0 keeps everything.
        with self.db() as db:
            return db.execute("DELETE FROM events WHERE at < ?", (self.clock() - days * 86400,)).rowcount

    # ── Reading ──────────────────────────────────────────────────────────────
    def events(self, since=None, until=None, kinds=None, audience="staff", limit=500):
        """Events visible to `audience` ("staff", "family" or "ember"), oldest first."""
        clauses, args = ["at >= ?", "at < ?"], [since or 0, until or self.clock() + 1]
        if kinds:
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            args += list(kinds)
        if audience == "family":
            shared = self.family_kinds()
            if not shared:
                return []
            clauses.append(f"private = 0 AND kind IN ({','.join('?' * len(shared))})")
            args += shared
        elif audience == "staff":
            clauses.append(f"(private = 0 OR kind IN ({','.join('?' * len(SAFETY_KINDS))}))")
            args += list(SAFETY_KINDS)
        elif audience != "ember":
            raise ValueError("audience must be staff, family or ember")
        with self.db() as db:
            rows = db.execute(f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY at, id LIMIT ?",
                              (*args, int(limit))).fetchall()
        return [self.row(r, audience) for r in rows]

    def row(self, r, audience):
        event = {"id": r["id"], "at": iso(r["at"]), "kind": r["kind"], "status": r["status"],
                 "summary": r["summary"], "category": r["category"],
                 "data": json.loads(r["data"]) if r["data"] else None}
        # A private safety event reaches staff as its summary only, without the resident's words.
        if r["quote"] and not (r["private"] and audience != "ember"):
            event["quote"] = r["quote"]
        if r["handled_at"]:
            event["handled_by"], event["handled_at"] = r["handled_by"], iso(r["handled_at"])
        return event

    def exceptions(self, day=None):
        """Today's list for staff: everything still needing a look, plus open items from earlier days."""
        start, end = day_bounds(day, self.clock)
        today = [e for e in self.events(start, end) if e["status"] in EXCEPTIONS]
        earlier = [e for e in self.events(0, start) if e["status"] == "open"]
        return earlier + today

    def metrics(self, since=None, until=None):
        """Pilot numbers: what Ember did, and how quickly staff closed what it raised."""
        since, until = since or 0, until or self.clock() + 1
        with self.db() as db:
            rows = db.execute("SELECT kind, status, at, handled_at FROM events WHERE at >= ? AND at < ?",
                              (since, until)).fetchall()
        by_kind = {}
        for r in rows:
            counts = by_kind.setdefault(r["kind"], {})
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        waits = [(r["handled_at"] - r["at"]) / 60 for r in rows if r["handled_at"]]
        return {"since": iso(since), "until": iso(min(until, self.clock())), "events": len(rows),
                "by_kind": by_kind, "handled": len(waits),
                "median_minutes_to_handle": round(statistics.median(waits), 1) if waits else None}

    # ── Consent ──────────────────────────────────────────────────────────────
    def family_kinds(self):
        """Kinds the resident agreed to share with family. Nothing until they agree."""
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='family_kinds'").fetchone()
        return json.loads(row["value"]) if row else []

    def set_family_kinds(self, kinds, recorded_by):
        kinds = sorted(set(kinds))
        if set(kinds) - set(KINDS):
            raise ValueError("Unknown kinds")
        consent = {"kinds": kinds, "recorded_by": text(recorded_by, 120, "recorded_by"), "at": iso(self.clock())}
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('family_kinds', ?)", (json.dumps(kinds),))
            db.execute("INSERT OR REPLACE INTO settings VALUES ('family_consent', ?)", (json.dumps(consent),))
        forward("/api/device/care-consent", consent)
        return consent

    def setting(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))


def cloud_enabled():
    return enabled() and os.getenv("EMBER_CARE_CLOUD", "1") != "0"


def forward(endpoint, payload, staff_visible=True):
    """Queue for the facility page. Private non-safety events never leave the robot."""
    if not (staff_visible and cloud_enabled()):
        return
    try:
        import baymax_cloud
        baymax_cloud.notify(endpoint, payload)  # No-op without BAYMAX_DEVICE_KEY.
    except Exception as error:
        print(f"[CARE] Could not queue for the facility page: {type(error).__name__}", flush=True)


log = CareLog()


def record(kind, summary, **fields):
    """For features: record when facility mode is on; never raise into a conversation."""
    if not enabled():
        return None
    try:
        return log.record(kind, summary, **fields)
    except Exception as error:
        print(f"[CARE] Could not record a {kind} event: {type(error).__name__}", flush=True)
        return None
