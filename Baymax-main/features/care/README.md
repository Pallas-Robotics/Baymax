# Care log: what facility staff (and, with consent, family) see

Facility mode for senior living: one Ember per resident's apartment. Features record
what happened as **care events**. Staff work from a page on the company website
(tadashirobotics.com), and the robot keeps its own copy. Off by default
(`EMBER_CARE_ENABLED=0`). Ember stays non-medical: the log refuses anything outside
its fixed kinds, so there's nowhere to put medication, diagnoses or clinical readings.

## How features use it
```python
from features import care
care.record("request", "Fresh towels", status="open", quote="Could I get some towels?", category="housekeeping")
```
- `record` does nothing when facility mode is off, and never raises into a conversation.
- **Kinds:** `check_in`, `request`, `help_request`, `night_wake`, `fall`, `activity`,
  `meal`, `note`, `flag` (`store.KINDS`).
- **Statuses:**
  - `ok` and `info` are for the record;
  - `attention` (staff should look), `no_answer` (an expected reply never came) and
    `open` (staff should act) put the event on the **exceptions list**;
  - staff move these to `handled`.
- `quote` is the resident's own words, when there are any. `data` is a small JSON object
  for feature-specific detail.
- `care.log.keep_private(id)`: the resident asked to keep it between them and Ember.

The API (`record`, `log.handle`, `log.events`, `log.keep_private`, `forward`) is plain
Python and knows nothing about Gemini or how tools are dispatched. This foundation
registers no Gemini tools; features that do (check-ins, requests, flags) call it from
their own handlers, whichever tool runner invokes them.

## Who sees what
| Event | Staff | Family |
|---|---|---|
| Ordinary event | Yes | Only kinds the resident agreed to share (none until they agree) |
| Private event | No | No |
| Private **safety** event (`fall`, `help_request`, `check_in`) | The summary, never the quote | No |

Staff can't act on a hidden fall or an unanswered check-in, so safety events always
reach them. Consent is recorded by staff after asking the resident
(`POST /api/care/consent` with `confirmed_with_resident: true`), with who recorded it
and when.

## Inputs and outputs
- **Inputs:** `care.record(...)` calls from features (check-ins, requests, falls, …).
- **Robot storage:** SQLite at `BAYMAX_DATA_DIR/care_log.sqlite3`. Kept 90 days
  (`EMBER_CARE_RETENTION_DAYS`; 0 keeps everything). Git-ignored and not encrypted:
  use the robot's normal disk protections.
- **Facility page (main output):** staff-visible events, handled marks, private marks and
  consent changes go to tadashirobotics.com through `baymax_cloud`'s store-and-forward
  uplink. It saves to disk first, retries and survives restarts. Private non-safety
  events never leave the robot. It needs the unit's `BAYMAX_DEVICE_KEY`;
  `EMBER_CARE_CLOUD=0` keeps everything on the robot. The website side is specified in
  `docs/facility/SITE_CONTRACT.md`.
- **Robot page (fallback):** `/care` on the robot's dashboard (`baymax_app.py`, port 5000).
  It uses the existing paired accounts. Accounts whose email is in `EMBER_STAFF_EMAILS`
  are staff; every other paired account is family. Routes:
  - `/api/care/today` and `/api/care/events`;
  - `/api/care/events/<id>/handled`, `/api/care/metrics` and `/api/care/export` (staff only);
  - `/api/care/consent`.
- **Morning email:** set `EMBER_CARE_DIGEST_TIME=09:30` to email staff accounts that paired
  the robot the exceptions list and yesterday's counts. It goes out once a day, through the
  dashboard's existing Gmail SMTP settings. **It contains summaries only, never the
  resident's words**, because email leaves the robot.
- **Pilot metrics:** counts by kind and status, how many were handled, and the median
  minutes to handle (`/api/care/metrics`, `/api/care/export`). Ember's side of the
  pilot time study.

## External data flows
- tadashirobotics.com: staff-visible events (summaries, quotes and small detail objects).
- Gmail SMTP: the digest summaries, only if a digest time is set.
- Nothing goes to Gemini from here. Features that call Gemini document that themselves.

## Enabling
```
EMBER_CARE_ENABLED=1
EMBER_STAFF_EMAILS=nurse@facility.org,frontdesk@facility.org
EMBER_CARE_DIGEST_TIME=09:30        # optional email
EMBER_CARE_CLOUD=1                  # default; 0 keeps the log on the robot
```
Restart the runtime and the dashboard after changing these.

## Disabling
`EMBER_CARE_ENABLED=0` (the default): `record` does nothing, `/care` and `/api/care/*`
return 404, no digest thread starts and nothing is forwarded. `EMBER_CARE_CLOUD=0` keeps
facility mode on the robot only.

## Tests
From `Baymax-main`, `python -m unittest tests.test_care` (or from the repository root,
`python tests/run.py --feature care`):
- kinds and limits;
- the exceptions list;
- handling;
- consent and the privacy rules;
- what is sent to the facility page;
- the digest;
- the robot's routes, including staff and family access.

No network, Gemini or devices are used.
