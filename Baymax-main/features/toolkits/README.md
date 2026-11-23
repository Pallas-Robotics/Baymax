# Ember toolkit

Reusable features live here; they never open microphones or cameras.

## Experimental public tools

This combined branch includes #7's desktop/debugging tools plus sixteen opt-in
public tools for personalized headlines, weather/air quality, currency reference
rates, holidays, earthquakes, background knowledge and book discovery. Enable the
experiment and choose groups in desktop Setup; tool activity appears in the live
monitor. Robot v10 supports the same public tools through its adapter. See
[inputs, providers, data flows, enabling/disabling and tests](EXPERIMENTAL.md).

## Conversational web discovery

When built-in search is enabled on Gemini 2.5, an explicitly zero thinking budget
is raised to 128 tokens, adapting Joe Kojima's `d2cca1f` fix. Zero thinking with
search plus function tools reproduced a session close on the volume command;
128 allowed both repeated volume tests to pass while retaining search. Existing
nonzero budgets are preserved. Search off retains the original budget. This can
add reasoning latency/quota compared with zero thinking; it does not add another
session, model or provider.

The existing Gemini Google Search tool can be used without an explicit search
request when a public topic in an engaged conversation merits a relevant lookup.
Ember is guided to share one sourced finding, connect it to the person's interest,
and contribute a reaction or invite their view. It avoids research during venting
or ongoing storytelling, repeated searches, and invented sources or familiarity.
Queries should contain public topic terms, not personal disclosures or transcripts;
Google receives the query through Gemini's existing built-in search integration.
No new provider, dependency, background worker or recording is added.
`BAYMAX_GOOGLE_SEARCH=0` or `BAYMAX_TOOLKIT_ENABLED=0` disables this search tool.
Offline coverage is in `tests/test_toolkit.py` (registration, idempotence and
disabled behavior); relevant results, latency and natural spoken follow-ups still
require live evaluation.

## Location and weather

- `location.py`: `Location.get()` estimates city/region via HTTPS IPWho (`ipwho.is`), cached for one hour. It does not return or save the public IP. `set_city(city, country_code, region)` resolves a city explicitly supplied by the user through Open-Meteo geocoding. Multiple results require clarification; nothing changes until one place matches.
- `weather.py`: `Weather.get(units)` returns Open-Meteo current model-based conditions and today's forecast with timestamps, units and provider attribution. Cache lasts ten minutes; new coordinates or units have separate entries. Service failures return an error, not invented or expired conditions.
- `gemini_tools.py`: declares `get_location`, `set_location`, and `get_weather` for Gemini Live, dispatches HTTP work off the audio event loop, and preloads default location/weather in a background thread at startup. That thread then keeps them cached so a weather question never waits on a fetch: the weather is fetched again every 9 minutes (`WEATHER_REFRESH_S`, before the 10-minute copy expires; in each unit asked for at the user's location, Celsius at least) and the IP location every 54 minutes (`LOCATION_REFRESH_S`; not while a city the user gave is in use). A failed refresh keeps the copy that is still valid; routine refreshes add no monitor events. Weather questions go straight to `get_weather`, which finds the location itself; `get_location`'s result carries a note saying it has no weather, because Gemini sometimes stopped after it and guessed the conditions.

No extra API key or package is required beyond the existing `requests` and Gemini SDK. No location data is written to disk by these tools. Confirmed cities last for the current process only. Gemini's existing transcript/recording behavior still applies.

IP location is approximate and can be wrong, particularly with a VPN or mobile network. It cannot establish an address, indoor room, GPS position or exact user whereabouts. A confirmed city refers to the city centre. Neither demo context nor old conversation notes establish the current location.

### Data sent to services

IPWho sees the requesting device's public IP. Open-Meteo receives approximate coordinates for weather and user-provided city/region queries for geocoding. While the toolkit is on, Ember sends these lookups on its own to keep the cache fresh: about 27 IPWho and 160 Open-Meteo forecast requests a day per robot (more if both temperature units are used), whether or not anyone asks. Tool results (including approximate coordinates) go to Gemini so it can speak the answer. Tools do not send audio, images, Gemini keys or email credentials to these providers.

### Try it

Start Ember normally and ask “Where am I?” or “What's the weather?” Correct the estimate with “I'm in [city], [state/region], [country]”, then ask about weather again. Ask for Celsius or Fahrenheit as desired. Stop Ember using the existing stop procedure.

Run offline tests: `python -m unittest discover -s tests`.

Set `BAYMAX_TOOLKIT_ENABLED=0` in the private `.env` to disable public tool declarations, added instructions and preloading. Independent eye commands remain available when the MVP eye session is enabled; `BAYMAX_EYES_ENABLED=0` disables those. The Gemini model and original audio/video pipeline have not changed.

### Provider references and deployment

- [IPWho API documentation](https://ipwhois.io/documentation)
- [Open-Meteo weather API](https://open-meteo.com/en/docs)
- [Open-Meteo geocoding API](https://open-meteo.com/en/docs/geocoding-api)
- [Gemini Live function calling](https://ai.google.dev/gemini-api/docs/live-api/tools)

The Open-Meteo public endpoint is for non-commercial use under its current service terms. A commercial Ember deployment needs the appropriate provider plan and deployment configuration; this developer implementation uses the public endpoints. Do not silently switch a fleet to the development endpoint. Review provider terms and rate limits before production use.

### Feature review protocol

Every added/changed toolkit follows [TESTING_PROTOCOL.md](../../TESTING_PROTOCOL.md). Add `tests/test_toolkit*.py`, run `python check_release.py`, and document normal, invalid/ambiguous, failure/timeout, core integration and disabled behavior. Provide separate core and toolkit results in the PR.

## Audiobooks (LibriVox)

One `audiobook` tool (`play` with title/author, `pause`, `resume`, `stop`,
`next`, `status`). `librivox.py` searches the Internet Archive and downloads one
chapter at a time to a temp cache (search/ranking adapted from PR #2). The cache
keeps only the chapter or track being played, since it is in `/tmp`, which is RAM
on the Orange Pis.
`audiobooks.py` decodes with `miniaudio` (`requirements-pc.txt`) on a feeder
thread into a ~2 s buffer; Ember's speaker callback mixes it in through the
audio cleanup's `mix_output`, so echo cancellation removes the book from the mic
and the book holds while Ember speaks. Status reports the chapter being heard.
Volume: `BAYMAX_BOOK_VOLUME` (0-1, default full; also the `volume` action's
0-100 %), times `BAYMAX_BOOK_BOOST` (default 1.25, so full volume is 25% louder
than the recording; loud peaks are clipped). Set the boost to `1` for the
recording's own level.

Book mode: echo cancellation leaves enough narration that Gemini answered the
narrator as the person. While a book plays, `gate` (in `processing.input_gates`)
withholds the mic from Gemini and feeds it to `wakeword.py`, an offline Vosk
listener (`vosk==0.3.45`, small English model ~41 MB in `models/`, gitignored;
fetch once with `python features/toolkits/wakeword.py --download`). "Hey Ember",
"Ember stop/pause", or a lone "stop"/"pause" pauses the book; the book also
pauses after every chapter. Either way Ember gets an `[AUDIOBOOK]` note naming
the book, author and chapter so it can talk about it, and `resume` continues.
Notes go to the current Gemini session: Google resets each connection after
about 10 minutes, so the player re-attaches to every new session through
`fall_detection.session_tasks`, not only to the one whose tool call started it.
Pairs count in either order ("Ember, hey"), "hey/okay stop" too, and across a
pause of up to 3 s (Vosk often splits "hey | Ember"). Any wake word heard on its
own lowers the book to 30% for 4 s (`DUCK_GAIN`/`DUCK_SECONDS` in
`audiobooks.py`) so the rest of the phrase is heard over it; narration that
sounds like a wake word causes the same short dip. Volume changes ramp over one
speaker block, so ducking and "louder" never click.
Without Vosk or the model the mic stays open (the old behaviour). Rules were
tuned on LibriVox narration and on ember-002 attempts (see the `wakeword.py`
docstring). No bookmarks yet.
Data sent: search terms and file requests to archive.org, plus the one-time
model download from alphacephei.com; wake words run locally.
Tests: `tests/test_toolkit_audiobooks.py` (offline fakes).

## Looking closely (`look_closely.py`)

When someone asks Ember to look at, read or identify something ("what am I
holding?", "read this letter", "is this expired?"), Gemini calls `look_closely`.

- **Why:** the regular camera frames to Gemini are 320x240 at JPEG quality 50,
  every 3 s, which is too soft for text.
- **What it sends:** the newest frame from the existing camera loop (kept
  through `fall_detection.frame_observers`; no camera is opened and capture size
  is unchanged). It goes at full capture size (640x480 on the MVP; anything over
  1280 px is scaled down) and quality 90, once, on the same
  `send_realtime_input(video=...)` input the 3 s frames use. The tool result
  then says the photo was sent.
- **Hints:** a local blur check (variance of the Laplacian below 60) and a
  brightness check (mean below 40) add `hints`, so Ember asks the person to hold
  it closer or steadier, or to turn on a light.
- **Errors:** no frame, a frame older than 2 s (camera stopped) or a failed send
  return an error, and Ember says it can't see right now.
- **Prompt:** `[LOOKING CLOSELY]` tells Ember to read only what it can see,
  never guess words, and never give dosing advice from a medicine label (it
  reads the label and suggests their pharmacist or doctor).
- **Data sent:** one extra JPEG per request, to Gemini, which already receives
  the camera feed. Nothing is stored. Session media resolution is unchanged.
- **Disable:** `BAYMAX_LOOK_CLOSELY=0` (or `BAYMAX_TOOLKIT_ENABLED=0`).
- **Tests:** `tests/test_toolkit_look_closely.py`: size and quality, scaling,
  blur/dark hints, no or stale frame, send failure, photo sent before the tool
  response, disabled, declaration and SDK validation, camera-hook registration.

## Radio and recordings (`listen.py`)

One `listen` tool plays live internet radio (`play_radio` with a genre, station
name, or a `public_radio` stationuuid as `station_id`, optional `country_code`)
and openly licensed recordings from the Internet Archive (`play_recording` with
what to hear). `pause`, `resume`, `stop`, `next` (recordings), `status` and
`volume` control whatever is playing, an audiobook included.

- **Shared player:** both run through the audiobook `Player` (`audiobooks.py`),
  which now plays a *program*: a book, a station or a recording. Only one plays
  at a time, and starting one replaces the other. Like books, playback is mixed into Ember's
  speaker stream (echo reference included), holds while Ember speaks, withholds
  the mic from Gemini, and pauses on the offline wake words with a `[RADIO]` or
  `[RECORDING]` note to Gemini. `BAYMAX_BOOK_VOLUME` sets the starting volume
  for all of them; `BAYMAX_BOOK_BOOST` lifts audiobooks only (radio and music are
  already loud), and a heard wake word briefly lowers any of them.
- **Radio:** `find_stations` asks Radio Browser (de1 mirror, most-listened first,
  tag search then name search) and keeps stations whose codec is MP3, Ogg or
  FLAC, not HLS, and passing Radio Browser's last check. Before playing, the
  stream is opened to confirm its Content-Type. miniaudio decodes it
  incrementally on the feeder thread through a `StreamableSource` (no new
  dependency). AAC/AAC+ and HLS stations are skipped with a clear error, since
  miniaudio has no AAC decoder. Stream URLs are third-party data, so each URL
  and redirect must be http(s) and resolve only to public internet addresses
  (never the robot's home network). Up to 3 checked redirects are followed;
  ICY metadata is not requested.
- **Stalls and drops:** 10 s without data or a dropped connection reconnects
  (2/4/8 s backoff). After 3 failed reconnects Ember is told the station stopped.
  Buffering is the player's ~2 s. Paused radio is not buffered; `resume`
  reconnects to the live stream.
- **Recordings:** an Archive full-text search (`mediatype:audio`, LibriVox
  excluded) is ranked by title match and popularity. Only items whose license
  URL is public domain/CC0, CC BY or CC BY-SA are used (the Open Definition
  licenses; any NC or ND item is skipped), and the item's own metadata is
  checked again. MP3 tracks (at most 100) download one at a time to the
  audiobook cache and play straight through; the cache keeps only the track
  playing. A track is downloaded whole before it plays, so a long single-file
  item stays in `/tmp` (RAM on the Orange Pis) while it plays: an hour of
  128 kbps MP3 is about 58 MB. Status gives the title, creator,
  license and archive.org page so Ember can attribute it. Licenses are as stated
  by the uploader.
- **Check-in:** after `BAYMAX_LISTEN_CHECK_MIN` minutes (default 30; 0 never)
  of radio or recordings, playback pauses and Ember asks whether to keep
  listening. This way nobody is stuck with a closed mic.
- **Fall check-ins and due reminders** don't wait for that: while anything
  plays, the mic is withheld from Gemini and only the pause words get through,
  so a possible-fall check-in (built-in v10 or toolkit) and a due reminder first
  pause the book, station or recording (`audiobooks.hold_for_answer`) and tell
  Gemini what paused. Reminder follow-ups, family messages and greetings still
  wait until playback pauses or stops.
- **Enable/disable:** `BAYMAX_LISTEN_ENABLED=0` removes the tool and its prompt
  section. It is on by default, and needs `BAYMAX_TOOLKIT_ENABLED`. `public_radio`
  still never returns stream URLs to Gemini.
- **Data sent:**
  - to Radio Browser: the search term, country code or station ID;
  - to the chosen station's server: a normal HTTP stream request;
  - to archive.org: search words and file requests.

  No account, mic audio or transcript is sent.
- **Tests:** `tests/test_toolkit_listen.py` (fake network/decoder: playback,
  wake pause and reconnecting resume, check-in, drops, unplayable codecs, open
  licenses, public-address and redirect checks, one-at-a-time, wiring and
  switch). Existing `tests/test_toolkit_audiobooks.py` runs unchanged.

## Starting conversations (`proactive.py`)

Ember speaks first in two cases: a friendly two- or three-sentence hello once per
run, four seconds after the first Gemini session starts
(`BAYMAX_STARTUP_GREETING=0` disables), and a
greeting on new eye contact (off by default; `BAYMAX_PRESENCE_GREETING=1` enables:
on ember-002 the mic stopped reaching Gemini for 144 s right after it started,
alongside pose fall detection on an i5-8200Y). Eye
contact uses MediaPipe Face Landmarker (`mediapipe`, model ~3.6 MB downloaded
once from storage.googleapis.com to `.models/`, checksum-pinned) on the existing
camera frames, which reach it through `fall_detection.frame_observers`; no new
camera stream. Face and pose inference run in a child process
(`features/vision_process.py`): in Ember's own process MediaPipe held Python's GIL,
starving the mic callback and Gemini I/O. Only frames go in and landmark tuples
come out over local pipes; nothing leaves the device. If the face child cannot start,
eye contact reports unavailable. If the core's pose child cannot start or
dies, it is started again once, on the CPU; video continues either way. A look counts when the face is turned to the camera, both irises
are centred, the face is within about 1-1.5 m (detector limit) and it lasts
~0.8 s after nobody looked for 30 s. The dashboard timeline shows "Eye contact
started" and "Eye contact ended" (with why it did or did not greet). It only
greets when nobody has spoken for
`BAYMAX_PRESENCE_QUIET_S` (120), at most every `BAYMAX_PRESENCE_COOLDOWN_S`
(600; the startup hello counts), not while Ember is talking or a book plays.
Runs at `BAYMAX_PRESENCE_FPS` (4; ~8 ms/frame on a desktop CPU) while a face is
in view and drops to 1 fps when nobody is there. The startup note
includes the device's current local date/time, UTC offset, timezone name and a
matching greeting, using the standard-library clock (no new dependencies).
Timezone/offset metadata is internal context; the spoken greeting omits it.
It asks Gemini to mention the local AM/PM time, notice a clearly visible detail in the
existing video, and ask one relevant question about what's happening there.
It does not prompt offers of reminders or other tasks; the general persona
offers capabilities only when they directly address an expressed need.
No sleepy waking-up roleplay or humming, invented surroundings, presumed moves
to a new place, or inferred physical location from a timezone. If the view is
unclear/unavailable, ask where Ember is or what's happening. City/state names need
user confirmation. Correct device clock/timezone configuration is required.
Data sent: the `[STARTUP]`/`[PRESENCE]` text note to the existing Gemini session;
no extra capture, provider request or persisted scene is added. The four-second
delay gives the existing three-second video sender time for its first frame;
it cannot guarantee a camera is available or a model follows the prompt.
Tests: `tests/test_toolkit_proactive.py` (fake faces, clocks, sessions, local
AM/PM/timezone/daypart boundaries, once-per-run greeting and reconnect retry).
Live robot voice, scene grounding and missing-camera checks remain pending.

## Gentle daily check-ins (`checkins.py`)

Ember checks in like a friend up to three times a day: a morning "How did you
sleep?", an afternoon "Have you had some water today?" and an evening "How was
your day?". There is at most one attempt per window per day. If nobody answers,
it is not repeated.

**When Ember checks in (all must be true):**
- **Not at boot.** Ember has been connected for at least
  `BAYMAX_CHECKIN_UPTIME_S` (1200). A check-in is never the startup hello.
- **Inside a window**, on the device-local clock:
  - `BAYMAX_CHECKIN_MORNING` (08:00-11:00)
  - `BAYMAX_CHECKIN_AFTERNOON` (14:00-16:30)
  - `BAYMAX_CHECKIN_EVENING` (18:00-20:30)

  `0` turns a window off.
- **Quiet and likely not alone.** Nobody, Ember included, has spoken for at
  least `BAYMAX_CHECKIN_QUIET_S` (300), and the person last spoke no more than
  `BAYMAX_CHECKIN_AROUND_S` (10800) ago. Recent conversation is the only
  "someone is home" signal that needs no extra camera work. Only the person's
  own transcripts count for it, not Ember's (the startup hello after an
  unattended reboot, an unanswered reminder), so an empty room (or nobody
  since boot) hears nothing.
- **Not busy.** Ember isn't speaking, thinking or calling a tool, and no
  audiobook is playing.
- **Not disengaged.** After "Ember disengage" Gemini hears only silence, so,
  like greetings, check-ins wait until the person says "Ember engage".

**Behaviour:**
- The note asks for one warm sentence, no medical advice, and to drop it if the
  person doesn't want to chat.
- "Done today" is kept in `toolkit_checkins.json` under `BAYMAX_DATA_DIR`
  (gitignored), so a restart doesn't ask again. A failed send is retried later
  in the window.

**Other details:**
- **Disable:** `BAYMAX_CHECKINS=0` (or `BAYMAX_TOOLKIT_ENABLED=0`).
- **Data sent:** only the `[CHECK-IN]` text note to the existing Gemini session.
  Answers are ordinary conversation; nothing is saved or sent elsewhere.
- **Runs through** `fall_detection.session_tasks`; `proactive.py` is unchanged.
- **Tests:** `tests/test_toolkit_checkins.py` (fake clocks, session and state
  file), plus the shared check that no session task blocks the event loop.

## Optional fall detection

An actual fall-alert dispatch now notifies the eye controller to show surprise,
overriding model expressions and delayed thinking. Missing/disabled eyes do not
prevent the existing check-in. No external notification is added; fake-alert
dispatch and priority tests cover the integration. See [eye behavior](../eyes/README.md).

Enable the experimental fall-detection checkbox in Setup and restart Ember. In v10 with camera pose tracking on (the default) it reuses the core camera thread's pose instead of running a model of its own, and that pose runs in the core's pose process, on the Orange Pi 5's NPU once [features/pose](../pose/README.md) is set up there (MediaPipe on the CPU otherwise; its inference time includes the round trip to that process). With v10's built-in fall detection also on (the default) that is a second detector, so one fall can give two check-ins; in v8, or with pose tracking off, it runs its own CPU model on copies of the camera frames. It reports performance/state in the monitor and can ask a possible-fall check-in through Gemini; no external alerts are sent. See [fall inputs, dependencies, outputs and validation](FALL_DETECTION.md).

## Conversation discovery

Five additional public tools provide trivia, TV shows, poetry, radio
station discovery and recipes. See [CONVERSATION.md](CONVERSATION.md) for inputs,
provider limits, enablement and checks. They use the existing Discovery checkbox.

## Everyday tasks

[Timers, reminders and household lists](everyday/README.md) persist locally and use the existing Gemini session for spoken notifications.
