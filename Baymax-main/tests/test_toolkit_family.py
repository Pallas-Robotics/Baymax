"""Ember app link: inbox/sync against a fake site, messages, replies, the profile (offline)."""
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from features.toolkits.everyday.store import Store
from features.toolkits.family import tools as family
from features.toolkits.family.link import Link
from features.toolkits.family.store import FamilyStore

TZ = "America/New_York"
NOW = datetime(2026, 10, 22, 7, 0, tzinfo=ZoneInfo(TZ)).timestamp()


class FakeSite:
    """Records syncs; serves whatever inbox the test sets."""
    def __init__(self):
        self.inbox = {}
        self.syncs = []
        self.fail = False

    def get(self, url, headers, timeout):
        assert url.endswith("/api/device/inbox") and headers["Authorization"] == "Bearer 002.secret"
        return self.response({"poll_seconds": 15, "messages": [], "reminders": [], "cancels": [], "profile": None, **self.inbox})

    def post(self, url, json, headers, timeout):
        assert url.endswith("/api/device/sync")
        self.syncs.append(json)
        return self.response({"status": "ok"})

    def response(self, body):
        if self.fail:
            raise ConnectionError("offline")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


class FamilyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = NOW
        self.reminders = Store(Path(tmp.name) / "everyday.sqlite3", clock=lambda: self.now, tz=TZ)
        self.store = FamilyStore(Path(tmp.name) / "family.sqlite3", clock=lambda: self.now)
        self.site = FakeSite()
        self.summaries = []
        def summarizer(notes):
            self.summaries.append(notes)
            return f"Summary of: {notes}"
        self.link = Link(self.store, self.reminders, url="https://example.test", key="002.secret",
                         summarizer=summarizer, http=self.site)
        env = patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1", "EMBER_EVERYDAY_ENABLED": "1", "EMBER_FAMILY_ENABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("store", self.store), ("link", self.link)):
            p = patch.object(family, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(family.everyday, "store", self.reminders)
        p.start()
        self.addCleanup(p.stop)

    def test_inbox_is_applied_and_reported_back(self):
        voice = self.reminders.reminder("create", text="Nap", seconds=600)["id"]
        self.site.inbox = {
            "messages": [{"id": "m1", "sender_name": "Jane", "text": "Dinner at 6", "created_at": "2026-10-22T10:00:00Z"}],
            "reminders": [
                {"cloud_id": "c1", "text": "Pills", "start": "2026-10-22T08:00", "rule": {"freq": "daily"}, "created_by": "Jane"},
                {"cloud_id": "c2", "text": "Too late", "start": "2026-10-21T08:00", "rule": None, "created_by": "Jane"},
            ],
            "cancels": [{"cloud_id": None, "robot_id": voice}, {"cloud_id": "never-seen", "robot_id": None}],
            "profile": {"notes": "Ruth, 82, loves gardening", "updated_at": "2026-10-22T10:01:00Z"},
        }
        self.link.cycle()

        sent = self.site.syncs[0]
        self.assertEqual(sent["tz"], TZ)
        self.assertEqual([r["cloud_id"] for r in sent["rejected"]], ["c2"])
        self.assertEqual(sent["cancelled"], [{"cloud_id": None, "robot_id": voice}, {"cloud_id": "never-seen", "robot_id": None}])
        states = {r["cloud_id"] or r["text"]: r["state"] for r in sent["reminders"]}
        self.assertEqual(states, {"c1": "pending", "Nap": "cancelled"})
        self.assertEqual(sent["profile"], {"summary": "Summary of: Ruth, 82, loves gardening", "for_updated_at": "2026-10-22T10:01:00Z"})
        self.assertTrue(self.link.profile_changed.is_set())
        self.assertEqual(self.store.unread()[0]["text"], "Dinner at 6")

        # The same inbox again (the site hasn't caught up): nothing duplicated, nothing re-sent.
        self.link.cycle()
        again = self.site.syncs[-1]
        self.assertEqual(again["reminders"], [])
        self.assertEqual(again["profile"]["for_updated_at"], "2026-10-22T10:01:00Z")
        self.assertEqual(len(self.summaries), 1, "the same notes are not summarised twice")
        self.assertEqual(len(self.store.unread()), 1)
        self.assertEqual(len([r for r in self.reminders.reminder("list")["reminders"] if r["text"] == "Pills"]), 1)

    def test_offline_raises_and_keeps_everything(self):
        self.reminders.reminder("create", text="Tea", seconds=60)
        self.site.fail = True
        with self.assertRaises(ConnectionError):
            self.link.cycle()
        self.site.fail = False
        self.link.cycle()
        self.assertEqual([r["text"] for r in self.site.syncs[-1]["reminders"]], ["Tea"])

    def test_large_backlog_syncs_in_pieces(self):
        for i in range(20):
            self.reminders.reminder("create", text=f"R{i}", seconds=60 + i)
        self.link.cycle()
        self.assertEqual([len(s["reminders"]) for s in self.site.syncs], [15, 5])

    def test_no_key_means_no_link(self):
        self.assertFalse(Link(self.store, self.reminders, url="https://example.test", key="").enabled())

    def test_message_read_out_then_reply_goes_up(self):
        self.store.add_messages([{"id": "m1", "sender_name": "Jane", "text": "Call me back", "created_at": "x"}])
        session = AsyncMock()
        with patch.object(family.everyday, "available_to_speak", return_value=False):
            asyncio.run(family.deliver_messages(session))
        session.send_client_content.assert_not_called()
        with patch.object(family.everyday, "available_to_speak", return_value=True):
            asyncio.run(family.deliver_messages(session))
            asyncio.run(family.deliver_messages(session))
        self.assertEqual(session.send_client_content.await_count, 1)
        note = str(session.send_client_content.call_args)
        self.assertIn("[MESSAGE]", note)
        self.assertIn('"message_id": "m1"', note)
        self.assertIn("Jane", note)

        self.assertIn("error", family.execute("send_reply", {"message_id": "nope", "text": "Hi"}))
        self.assertIn("error", family.execute("send_reply", {"message_id": "m1", "text": " "}))
        sent = family.execute("send_reply", {"message_id": "m1", "text": "Will do, love you"})
        self.assertTrue(sent["queued"])
        self.assertEqual(sent["say"], "Got it, message sent.")  # Ember confirms out loud.
        # Asked "do you know my name?", Ember must answer from the profile, not deny it.
        self.assertIn("answer from this directly", family.PROFILE_HEADER)
        self.assertNotIn("James", family.PROFILE_HEADER)
        self.assertIn('"Got it,\nmessage sent."', family.INSTRUCTION)
        self.link.cycle()
        sent = self.site.syncs[-1]
        self.assertEqual([d["id"] for d in sent["delivered"]], ["m1"])
        self.assertEqual([(r["reply_to"], r["text"]) for r in sent["replies"]], [("m1", "Will do, love you")])
        self.link.cycle()
        self.assertEqual((self.site.syncs[-1]["delivered"], self.site.syncs[-1]["replies"]), ([], []))

    def test_profile_goes_in_the_instructions_and_mid_session(self):
        from features.toolkits import gemini_tools
        self.store.set_profile("Ruth, 82; call her Ruthie.", "t1")
        for toolkit in ("1", "0"):
            with patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": toolkit}):
                config = gemini_tools.extend_config({"system_instruction": "Base", "tools": []})
            self.assertIn("[ABOUT THE PERSON]", config["system_instruction"])
            self.assertIn("call her Ruthie", config["system_instruction"])
            names = {d["name"] for t in config.get("tools", []) for d in t.get("function_declarations", [])}
            self.assertEqual("send_reply" in names, toolkit == "1")

        session = AsyncMock()
        with patch.object(family.everyday, "available_to_speak", return_value=True):
            asyncio.run(family.refresh_profile(session))
            session.send_client_content.assert_not_called()
            self.link.profile_changed.set()
            asyncio.run(family.refresh_profile(session))
        kwargs = session.send_client_content.call_args.kwargs
        self.assertFalse(kwargs["turn_complete"])
        self.assertIn("call her Ruthie", str(kwargs["turns"]))

    def test_a_profile_change_waits_until_ember_is_free(self):
        # Like a message: a note sent while Gemini is replying could cut the reply off.
        self.store.set_profile("Ruth, 82; call her Ruthie.", "t1")
        self.link.profile_changed.set()
        session = AsyncMock()
        with patch.object(family.everyday, "available_to_speak", return_value=False):
            asyncio.run(family.refresh_profile(session))
        session.send_client_content.assert_not_called()
        self.assertTrue(self.link.profile_changed.is_set())     # still pending
        with patch.object(family.everyday, "available_to_speak", return_value=True):
            asyncio.run(family.refresh_profile(session))
        self.assertIn("call her Ruthie", str(session.send_client_content.call_args))
        self.assertFalse(self.link.profile_changed.is_set())

    def test_session_task_is_registered(self):
        from features.toolkits import fall_detection
        self.assertIn(family.session_task, fall_detection.session_tasks)


if __name__ == "__main__":
    unittest.main()
