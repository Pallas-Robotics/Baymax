# Facility page on tadashirobotics.com: what the website needs

**For:** whoever works on the website (the Cloudflare Worker + D1 app behind tadashirobotics.com,
the same one that serves the fleet console in Baymax #14 and the device routes in `baymax_cloud.py`).

**Goal:** facility staff sign in once and see every Ember in their building on one page: who to
check on, open requests, and drafts to sign. All with as little change to how the facility
already works as possible.

## 1. What robots send
Robots post through `baymax_cloud` with `Authorization: Bearer <serial>.<key>`, the same device
keys as `/api/device/fall`. Delivery is at-least-once and in order per robot, so **every
handler must be idempotent** on `(serial, id)`. Reply 2xx when stored. Reply 400/401/403/404
only for requests that can never succeed: the robot drops them. Reply 5xx/429 to make it retry.

| Endpoint | Body | Site does |
|---|---|---|
| `POST /api/device/care-event` | `{id, at, kind, status, summary, category, data, quote?}` | Upsert on (serial, id) |
| `POST /api/device/care-handled` | `{id, handled_by, handled_at}` | Mark handled (also done on the site, §3) |
| `POST /api/device/care-private` | `{id, safety}` | `safety=false`: delete the event. `safety=true`: delete its quote only |
| `POST /api/device/care-consent` | `{kinds[], recorded_by, at}` | Store as the resident's family-sharing consent |

- `id` is the robot's local event ID: unique per serial and never reused.
- `at` is ISO 8601 with the robot's offset.
- `kind` is one of `check_in`, `request`, `help_request`, `night_wake`, `fall`, `activity`,
  `meal`, `note`, `flag`.
- `status` is one of `info`, `ok`, `attention`, `no_answer`, `open`, `handled`.
- `summary` is at most 300 characters, `quote` at most 500, and `data` is a JSON object
  under 4 KB.
- Features 2–5 add to `data` (for example a request's `route`, or a draft's text). Unknown
  keys must be stored and shown, not rejected.

## 2. Storage (D1)
```sql
CREATE TABLE facilities (id TEXT PRIMARY KEY, name TEXT NOT NULL, timezone TEXT NOT NULL);
CREATE TABLE facility_units (serial TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities(id),
  label TEXT NOT NULL);                     -- "Apt 214 · Rose M." as staff know the resident
CREATE TABLE facility_staff (email TEXT NOT NULL, facility_id TEXT NOT NULL REFERENCES facilities(id),
  role TEXT NOT NULL CHECK (role IN ('aide','nurse','maintenance','frontdesk','admin')),
  PRIMARY KEY (email, facility_id));
CREATE TABLE care_events (serial TEXT NOT NULL, id INTEGER NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL, summary TEXT NOT NULL, quote TEXT, category TEXT, data TEXT,
  handled_by TEXT, handled_at TEXT, received_at TEXT NOT NULL, PRIMARY KEY (serial, id));
CREATE INDEX care_events_at ON care_events(serial, at);
CREATE TABLE care_consent (serial TEXT PRIMARY KEY, kinds TEXT NOT NULL, recorded_by TEXT NOT NULL, at TEXT NOT NULL);
```
- Retain 90 days, like the robot, unless the facility agreement says otherwise.
- Every staff read and write goes into an access log (who, which unit, when). Facilities will
  ask for this.

## 3. The staff page (`/facility`)
- **Sign-in:**
  - email magic link, which needs no new password;
  - "Sign in with Microsoft" and "Sign in with Google", because most facilities run on one
    of them;
  - only emails in `facility_staff` get in, and they see only their own facility.
- **Today (the default view):** one list for the whole building, newest exceptions first.
  - Each entry shows the unit label, the summary, the time and a "Mark handled" button. The
    button records who and when, and is mirrored to the robot later through the fleet
    agent's commands (#14).
  - It's filterable by role: maintenance sees `request` with `data.route` = maintenance;
    aides see check-ins and help requests.
- **Unit view:** the timeline for one resident, plus the drafts to review (feature 4) and
  the flags (feature 5).
- **Shift print:** a one-page printable list for each shift. Many floors still run on paper.
- **Pilot report:** per-facility totals, time to handle and the export (§4).
- **Family view:** family accounts invited by staff see only the kinds in `care_consent`,
  for their own resident, and never quotes from private events. This could replace the
  robot-local family page later.

## 4. Fitting into the facility's existing software
Least friction first. Each step works without the next.

1. **Email they already read.** Morning exceptions digest and per-route request emails.
   - Most maintenance and work-order systems (TELS, Building Engines, MaintainX and
     others) accept tickets by email to a dedicated address. So routing a request to
     the "maintenance" address *is* the integration, with no API work.
   - The site keeps a route → address map per facility.
2. **Exports in the shapes they file.**
   - CSV for the time study and the facility's own spreadsheets.
   - PDF or print for notes that staff sign (feature 4), which they can upload to their
     EHR as an attachment.
3. **Their single sign-on**, so there's no new account to manage.
4. **Calendar in.** Facilities publish activity calendars (often an ICS or a PDF from
   their engagement software). Feature 3 reads the ICS or a CSV upload; a PDF needs a
   human upload.
5. **Later, direct EHR and CRM connections:**
   - PointClickCare, ALIS, Yardi or ECP for drafts and care-level flags;
   - Welcome Home or Sherpa for move-in sales.

   These need partner programs and contracts, so do them only once a pilot facility asks.

## 5. Not on the site
- Raw audio, transcripts of whole conversations, camera frames: none of these leave the robot
  through this path.
- Anything clinical: medication, diagnoses, vitals. The robot refuses these kinds.
