# Ember app link: messages, dashboard reminders, about the person

Connects the robot to the Ember app on tadashirobotics.com. Off unless the unit
has `BAYMAX_DEVICE_KEY` (and `EMBER_FAMILY_ENABLED` isn't `0`).

Every ~15 s `link.py` reads `GET /api/device/inbox` and reports through
`POST /api/device/sync` (15 rows of a kind per request, repeated until done):

| From the app | On the robot |
|---|---|
| A message | Kept in `toolkit_family.sqlite3`. At the next quiet moment (same rule as reminders) Ember reads it out, says who it's from and offers to reply. A reply is read back and sent only after the person confirms (`send_reply`). |
| A reminder, one-time or repeating | Added to the everyday reminder store, labelled "dashboard" with the sender's name. One the store refuses (e.g. a time in the past) is reported back with the reason. |
| A cancellation | Applied to that reminder, whether it was set on the app or by voice. |
| Notes about the person | Summarised by Gemini (`gemini-3.5-flash`, as session summaries are) into a short profile. It goes into Ember's instructions as `[ABOUT THE PERSON]` at every connection, is sent mid-conversation when it changes (at the next quiet moment, like messages: a note sent while Ember is replying could cut the reply off), and goes back to the app so the owner sees exactly what Ember uses. |

What goes back up: reminder definitions, states and next times; each
announcement with its check-in answer; which messages were read out; replies;
the profile summary; the robot's timezone. **Transcripts are never sent.**
Message text, replies, reminder text and the notes do go to the app's database
(the owner's choice, 2026-10-22).

Offline, everything waits in the local databases and syncs when the link is
back. Tests: `tests/test_toolkit_family.py` (fake site) and
`tests/test_toolkit_reminder_repeats.py`.

Joe Kojima's quiet-profile guard (ee940fa) is extended in the OPI4 candidate:
quietness is checked again after loading the profile, and failed/cancelled or
deferred deliveries stay pending. An edit arriving during a send remains pending
for the next poll. This is a local best-effort quiet check, not an atomic
guarantee about remote generation. No additional data or provider is introduced.
