"""Facility care log, its sharing rules, the facility-page uplink and the robot's own care routes (offline)."""
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from features.care import dashboard, digest, store
from features.care.store import CareLog

ON = {"EMBER_CARE_ENABLED": "1", "EMBER_STAFF_EMAILS": "Nurse@Example.com"}


class Clock:
    def __init__(self, at):
        self.now = datetime.fromisoformat(at).astimezone().timestamp()

    def __call__(self):
        return self.now


class CareLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock("2026-11-13T08:00:00")
        self.log = CareLog(Path(self.dir.name) / "care.sqlite3", self.clock)
        patcher = patch.object(store, "forward")
        self.forward = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def test_only_known_non_clinical_kinds_and_bounded_text(self):
        with self.assertRaises(ValueError):
            self.log.record("medication", "Took pills")
        with self.assertRaises(ValueError):
            self.log.record("note", "x" * 301)
        with self.assertRaises(ValueError):
            self.log.record("note", "ok", status="handled")
        with self.assertRaises(ValueError):
            self.log.record("note", "ok", data={"big": "x" * 5000})
        event_id = self.log.record("request", "Fresh towels", status="open", quote="Could I get towels?", category="housekeeping")
        [event] = self.log.events()
        self.assertEqual((event["id"], event["status"], event["quote"], event["category"]),
                         (event_id, "open", "Could I get towels?", "housekeeping"))

    def test_exceptions_today_plus_open_requests_from_earlier_days(self):
        self.log.record("request", "Light bulb out", status="open")
        self.log.record("note", "Mentioned a visit", status="info")
        self.clock.now += 86400
        self.log.record("check_in", "Slept badly, sounded low", status="attention")
        self.log.record("check_in", "Morning check-in fine", status="ok")
        self.assertEqual([e["summary"] for e in self.log.exceptions()],
                         ["Light bulb out", "Slept badly, sounded low"])

    def test_handle_records_who_and_when_once(self):
        event_id = self.log.record("request", "Towels", status="open")
        self.clock.now += 1800
        self.assertTrue(self.log.handle(event_id, "Maria"))
        self.assertFalse(self.log.handle(event_id, "Maria"))
        [event] = self.log.events()
        self.assertEqual((event["status"], event["handled_by"]), ("handled", "Maria"))
        self.assertEqual(self.log.metrics()["median_minutes_to_handle"], 30.0)
        self.assertEqual(self.log.exceptions(), [])

    def test_family_sees_nothing_until_consent_then_only_shared_kinds(self):
        self.log.record("activity", "Joined bingo")
        self.log.record("request", "Towels", status="open")
        self.assertEqual(self.log.events(audience="family"), [])
        self.log.set_family_kinds(["activity"], "Maria")
        self.assertEqual([e["summary"] for e in self.log.events(audience="family")], ["Joined bingo"])
        self.assertEqual(self.log.setting("family_consent")["recorded_by"], "Maria")
        with self.assertRaises(ValueError):
            self.log.set_family_kinds(["medication"], "Maria")

    def test_private_hides_from_family_and_staff_but_safety_reaches_staff_without_words(self):
        self.log.set_family_kinds(["activity", "check_in"], "Maria")
        chat = self.log.record("activity", "Talked about her son", quote="Don't tell anyone")
        safety = self.log.record("check_in", "Said she feels dizzy", status="attention", quote="I feel dizzy")
        self.log.keep_private(chat)
        self.log.keep_private(safety)
        self.assertEqual(self.log.events(audience="family"), [])
        [staff] = self.log.events(audience="staff")
        self.assertEqual(staff["summary"], "Said she feels dizzy")
        self.assertNotIn("quote", staff)
        self.assertEqual(len(self.log.events(audience="ember")), 2)

    def test_metrics_and_retention(self):
        self.log.record("check_in", "Fine", status="ok")
        self.log.record("check_in", "No reply", status="no_answer")
        self.assertEqual(self.log.metrics()["by_kind"], {"check_in": {"ok": 1, "no_answer": 1}})
        self.clock.now += 91 * 86400
        self.log.record("note", "New")
        self.assertEqual(self.log.prune(90), 2)
        self.assertEqual(self.log.prune(0), 0)

    def test_record_helper_is_off_by_default_and_never_raises(self):
        with patch.object(store, "log", self.log), patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(store.record("note", "Off"))
        with patch.object(store, "log", self.log), patch.dict(os.environ, ON):
            self.assertIsInstance(store.record("note", "On"), int)
            self.assertIsNone(store.record("medication", "Refused"))
        self.assertEqual([e["summary"] for e in self.log.events()], ["On"])


class UplinkTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = CareLog(Path(self.dir.name) / "care.sqlite3", Clock("2026-11-13T08:00:00"))

    def sent(self, actions, env=ON):
        with patch("dotenv.load_dotenv"):
            import baymax_cloud
        with patch.dict(os.environ, env, clear=True), patch.object(baymax_cloud, "notify") as notify:
            actions()
        return [(c.args[0], c.args[1]) for c in notify.call_args_list]

    def test_staff_visible_events_and_changes_go_to_the_facility_page(self):
        def actions():
            open_id = self.log.record("request", "Towels", status="open", quote="Towels please")
            self.log.record("note", "Private chat", private=True)
            self.log.record("fall", "Fall detected", status="attention", private=True, quote="I slipped")
            self.log.handle(open_id, "Maria")
            self.log.keep_private(open_id)
            self.log.set_family_kinds(["activity"], "Maria")
        sent = self.sent(actions)
        self.assertEqual([endpoint for endpoint, _ in sent],
                         ["/api/device/care-event", "/api/device/care-event", "/api/device/care-handled",
                          "/api/device/care-private", "/api/device/care-consent"])
        self.assertEqual(sent[0][1]["quote"], "Towels please")
        self.assertEqual(sent[1][1]["summary"], "Fall detected")
        self.assertNotIn("quote", sent[1][1])          # private safety event: no words leave the robot
        self.assertEqual(sent[3][1], {"id": 1, "safety": False})

    def test_nothing_is_sent_when_facility_mode_or_the_uplink_is_off(self):
        actions = lambda: self.log.record("request", "Towels", status="open")  # noqa: E731
        self.assertEqual(self.sent(actions, {}), [])
        self.assertEqual(self.sent(actions, {**ON, "EMBER_CARE_CLOUD": "0"}), [])


class DigestTests(unittest.TestCase):
    def test_email_lists_exceptions_without_the_residents_words(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(store, "forward"):
            clock = Clock("2026-11-12T10:00:00")
            log = CareLog(Path(folder) / "care.sqlite3", clock)
            log.record("check_in", "Fine yesterday", status="ok")
            clock.now += 86400
            log.record("check_in", "No answer to the morning check-in", status="no_answer")
            log.record("request", "Light bulb out in the bathroom", status="open", quote="My bathroom light is out")
            subject, body = digest.staff_digest(log, {"name": "Apt 214"}, datetime.fromtimestamp(clock.now).date())
        self.assertEqual(subject, "Ember Apt 214: 2 items to check today")
        self.assertIn("[No answer]", body)
        self.assertIn("Light bulb out in the bathroom", body)
        self.assertNotIn("My bathroom light is out", body)
        self.assertIn("Yesterday: check in: 1.", body)

    def test_digest_due_once_per_day_after_the_time(self):
        now = datetime(2026, 11, 13, 9, 45).astimezone()
        self.assertTrue(dashboard.digest_due(now, None, "09:30"))
        self.assertFalse(dashboard.digest_due(now, "2026-11-13", "09:30"))
        self.assertFalse(dashboard.digest_due(now, "2026-11-12", "10:00"))


class RobotRouteTests(unittest.TestCase):
    """The robot's own /care page and API in baymax_app (the facility page is on the website)."""
    def setUp(self):
        # Import without reading the developer's .env or writing a Flask secret next to the code.
        with patch("dotenv.load_dotenv"), patch.dict(os.environ, {"FLASK_SECRET_KEY": "test"}):
            import baymax_app
        self.app = baymax_app
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = CareLog(Path(self.dir.name) / "care.sqlite3")
        self.users = [{"id": "1", "username": "maria", "email": "nurse@example.com", "devices": [baymax_app.DEVICE_SERIAL]},
                      {"id": "2", "username": "son", "email": "son@example.com", "devices": [baymax_app.DEVICE_SERIAL]}]
        for p in (patch.object(store, "log", self.log), patch.object(store, "forward"),
                  patch.object(baymax_app, "_load_users", lambda: self.users), patch.dict(os.environ, ON)):
            p.start()
            self.addCleanup(p.stop)
        self.client = baymax_app.app.test_client()

    def login(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id

    def test_signed_out_and_facility_mode_off_are_refused(self):
        self.assertEqual(self.client.get("/api/care/today").status_code, 401)
        self.login("1")
        with patch.dict(os.environ, {"EMBER_CARE_ENABLED": "0"}):
            self.assertEqual(self.client.get("/api/care/today").status_code, 404)

    def test_staff_see_exceptions_and_close_them_family_see_only_shared(self):
        event_id = self.log.record("request", "Towels", status="open", quote="Towels please")
        self.log.record("activity", "Joined bingo")
        self.login("2")
        family = self.client.get("/api/care/today").get_json()
        self.assertEqual((family["role"], family["events"]), ("family", []))
        self.assertNotIn("exceptions", family)
        self.assertEqual(self.client.post(f"/api/care/events/{event_id}/handled").status_code, 403)
        self.assertEqual(self.client.post("/api/care/consent", json={"kinds": ["activity"], "confirmed_with_resident": True}).status_code, 403)
        self.login("1")
        staff = self.client.get("/api/care/today").get_json()
        self.assertEqual([e["summary"] for e in staff["exceptions"]], ["Towels"])
        self.assertEqual(self.client.post("/api/care/consent", json={"kinds": ["activity"]}).status_code, 400)
        self.assertEqual(self.client.post("/api/care/consent", json={"kinds": ["activity"], "confirmed_with_resident": True}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/care/events/{event_id}/handled").get_json(), {"ok": True})
        self.assertEqual(self.client.get("/api/care/today").get_json()["exceptions"], [])
        self.assertEqual(self.client.get("/api/care/export?days=1").get_json()["metrics"]["handled"], 1)
        self.login("2")
        self.assertEqual([e["summary"] for e in self.client.get("/api/care/today").get_json()["events"]], ["Joined bingo"])

    def test_page_is_served_without_data(self):
        page = self.client.get("/care")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Ember care log", page.data)
        page.close()


if __name__ == "__main__":
    unittest.main()
