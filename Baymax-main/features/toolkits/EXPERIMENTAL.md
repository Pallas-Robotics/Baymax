# Experimental public-information toolkit

This feature branch adds sixteen on-demand Gemini tools to the merged **v10 robot
runtime** and includes PR #7's **v8 desktop setup/testing tools**. It is an
experiment for review, disabled by default. Models, audio sample rates, capture pipelines, production alerts and startup services
are unchanged. Person/setting and memory-recall changes are scoped to PR #12.

## Enable and try

For desktop testing, download the entire combined branch and run the included
`devops/clickables` Setup for your OS. Enable **Public information tools**, choose
groups, Save, then use Monitor and Start Ember. The existing device/key controls,
transcript UI are inherited from #7; the optional CPU fall toolkit is added here. Public tool calls
appear as grouped name-only activity in the monitor; no arguments or results are
added to monitor telemetry. This PR is stacked on #7, which owns OS compatibility.

In the robot's private environment (`.env` for a source run, or the existing
`/etc/ember/secrets.env` for Docker), set:

```dotenv
EMBER_PUBLIC_TOOLKIT=1
EMBER_PUBLIC_TOOL_GROUPS=feeds,environment,reference,events,discovery
```

Restart the selected runtime in your test environment. No new provider API
keys or Python dependencies are required. Remove groups to disable that set of
tools, or set `EMBER_PUBLIC_TOOLKIT=0` to disable the whole experiment. Invalid
group configuration exposes no opt-in public tools. The `discovery` group is on
by default through its own `EMBER_DISCOVERY_ENABLED` switch (see CONVERSATION.md). Disabled tools do not fetch data.
The master `BAYMAX_TOOLKIT_ENABLED=0` switch disables public tools in either runtime.
There are no public-experiment requests on import, startup, or a timer: fetching is
on demand. The included desktop toolkit's existing location/weather startup
prewarm remains unchanged, including its documented IPWho request.
Headlines and environmental results cache for ten minutes, earthquakes for five,
currency/place/search results for one hour, and holidays for one day. Expired
results are never served as a fallback when a provider fails. Caches are memory-only,
shared across profiles; personalization happens after fetching public headlines.

Example requests:

- “What are the technology and market headlines?”
- “Use the feed profile nate. Follow technology and markets, with interests AI,
  semiconductors and Microsoft. Exclude crypto.”
- “Only give me stories matching my interests.” (`strict=true`)
- “Give me the headlines for nate.”
- “Add robotics to nate's interests.” (read the preferences, then replace that list)
- “Forget the feed preferences for nate.”
- “What's the weather in Berlin, Germany for the next three days?”
- “What's the air quality there?”
- “What's the daily USD to EUR reference rate?”
- “What are the US public holidays in 2026?”
- “What recent earthquakes have been reported?”
- “What is a semiconductor?” / “Find books about robotics.”

Conversational examples require live Gemini verification. The code handles the
tool arguments; the model must still select the right tools and clarification.

## Capabilities and data flows

All HTTPS providers see the robot's public IP and a generic Ember User-Agent.
Only the fields below are sent; no audio, images, email credentials or Gemini
API key are sent by these tools. Tool results and preference tool arguments go
to the existing Gemini session. Its normal transcript/memory behavior still applies.

| Group / tool | Provider and inputs sent | Output and limitations |
|---|---|---|
| `public_capabilities` | Local, no request | Enabled tool names, supported categories and source catalog |
| feeds / `public_headlines` | Fixed public RSS URLs for requested categories; **no profile or interest terms** sent | Up to ten dated headlines, links, source names, matching interests and partial-failure status |
| feeds / `public_feed_preferences` | Local JSON only; explicit profile ID and requested changes | Read/update/forget local preferences; omitted fields remain unchanged |
| environment / `public_find_place` | [Open-Meteo geocoding](https://open-meteo.com/en/docs/geocoding-api): city and optional country code | Up to five city-centre candidates; clarify ambiguity before weather/AQI |
| environment / `public_weather` | [Open-Meteo](https://open-meteo.com/en/docs): explicit coordinates and units | Model-based current conditions, units, three-day forecast, sunrise/sunset |
| environment / `public_air_quality` | [Open-Meteo / CAMS](https://open-meteo.com/en/docs/air-quality-api): explicit coordinates | US/European AQI and particulate concentrations, model timestamp and units; not medical advice |
| reference / `public_exchange_rate` | [Frankfurter](https://frankfurter.dev/): base and quote currency codes | Dated daily reference rate; not an intraday or tradable quote |
| events / `public_holidays` | [Nager.Date](https://date.nager.at/): country code and year | National/regional public holidays with applicability; not personal calendar access |
| events / `public_earthquakes` | [USGS](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php): fixed M2.5+ past-day feed | Newest reported events; not an emergency warning service |
| discovery / `public_knowledge` | [Wikipedia](https://www.mediawiki.org/wiki/API:Search): search query | Up to three brief encyclopedia excerpts and links; not breaking news |
| discovery / `public_books` | [Open Library](https://openlibrary.org/dev/docs/api/search): search query | Up to five titles/authors and links; no downloads or audiobook playback |

Headline categories: **world, technology, business, markets, science, health,
sports, entertainment, space**. Eight use BBC/CNBC RSS; space uses [NASA's feed](https://www.nasa.gov/rss-feeds/).
The registry in `feeds.py` contains the exact URLs. Categories are English-language
and globally/UK/US oriented, not comprehensive local-news coverage.

Feeds contain recent publisher-selected stories, not a searchable news archive.
Matching uses headline text and word boundaries (e.g. AI does not match “rail”),
with small documented aliases for AI, semiconductors and crypto. Explicit interests
rank matches first; strict mode excludes all nonmatches. Exclusions always remove
matching headlines. Duplicate titles/URLs are removed; articles without a usable
publication date, older than the requested window or in the future are excluded.
No matches are reported honestly, without filling strict feeds with unrelated news.
Provider outages are isolated per feed. Sources may change or withdraw endpoints.

## Personalization and persistence

Profile IDs are chosen explicitly by the user (lowercase letters/digits/`_`/`-`).
No face recognition or automatic speaker attribution is added. There is no profile
listing tool: a user must name their profile. This is not an authentication boundary;
people sharing the robot can request another known feed ID. With several people
present, Ember should ask whose feed they want.

Preferences are stored atomically in `BAYMAX_DATA_DIR/toolkit_feed_preferences.json`
(default: next to the runtime). The file is Git-ignored and excluded from the
Docker image. It holds only category, interest, exclusion and strict-mode settings,
for at most 32 profiles. There are no automatically inferred interests or browsing
histories. Reads don't create profiles, updates change only supplied fields, and
forget removes the named entry. The v10 core is the sole writer; a future dashboard
writer will need shared coordination. Corrupt/unwritable storage returns an error
and is not silently overwritten. Run only one core (desktop or robot) against a
given data directory at a time.

These preferences are deliberately separate from the **open** people-awareness
PR #8. It is not yet a dependency of merged main. When integrating with #8, reuse
its identity flow for confirmed person selection, add feed deletion to both voice
and dashboard forgetting, and explicitly decide the cloud/profile schema before
syncing preferences. Do not add fields that `normalize_profile()` discards.

## Runtime integration and other PRs

- `experimental.extend_config()` appends only enabled, uniquely named `public_*`
  tools; existing tool declarations and conversation settings are preserved. The
  registry can coexist with this PR's location/weather names without collisions.
- The v10 receive loop handles tool calls before checking `server_content`, since
  tool-only messages have no server content. Results preserve Gemini call IDs.
- The included v8 desktop adapter registers both the original weather tools and
  new public tools, preserving other declarations. Public calls use the same bounded
  dispatch and the existing monitor's start/end events; no v8 pipeline changes are
  added beyond documented toolkit hooks and the monitor/device foundation from #7.
- Blocking HTTP/disk work runs in a daemon worker thread with the per-tool limit
  in `time_limits.py` (5 s local, 8 s place/weather/air quality, 12 s other
  `public_*` lookups, 10 s `brain_game`; see the toolkit README), applied by the
  harness tool runner and by `run_call` itself, and a maximum of eight executed
  calls in one batch. HTTP reads use a five-second socket timeout and a two-MB
  response cap. Threads already in progress cannot be force-cancelled by the time
  limit; a slow peer may keep its worker alive until its socket operation finishes,
  but its late result is dropped and the daemon thread does not hold up shutdown.
  No capture streams are opened.
- HTTPS requests use a provider-host allowlist and reject redirects. Feed links
  are returned as data; arbitrary article URLs are never fetched. Feed XML rejects
  DTD/entity declarations. Text is treated as untrusted data in the tool instructions.
- #8/#9 change `_build_config()` and tool dispatch in v10. Integrating those PRs
  requires preserving their people/forgetting handler and routing only `public_*`
  calls here. This adapter is not a replacement for their handler. Register news
  in #8's enabled-capability description as well. #9's audio changes stay separate.
- Reminders #5 and audiobooks #2 target older runtimes; neither is copied into
  this experiment. Shared registration/routing is the future combination point.
- `.dockerignore` explicitly includes only toolkit Python sources. Service files,
  deployment channels and production enablement are unchanged.

## Tests and review

From `Baymax-main`, using Python 3.11+ (the current image uses 3.13):

```bash
python -m unittest discover -s tests
python check_release.py --report .local-review/combined-review.md
python -m features.toolkits.smoke --live
```

The first two commands are offline and use the included desktop environment
(requests, dotenv, NumPy and the Gemini SDK). Tests cover profile isolation,
restart, update/forget, corrupt/save-failure handling, ranking, strict/excluded
topics, date windows, deduplication, partial/all-source failure, invalid inputs,
caching, URL/redirect/size restrictions, each provider adapter, preserving existing
tools, result IDs and executing the real v10 config/receive function bodies with
hardware/Gemini dependencies replaced. They don't import the hardware runtime.

The explicit live smoke command sends **only synthetic requests**: all nine
headline categories, Berlin/DE and coordinates 52.52/13.41, USD/EUR, US holidays
in 2026, the fixed earthquake feed, “semiconductor” on Wikipedia and “robotics” on
Open Library. It never opens devices or calls Gemini, uses temporary preferences,
and prints only availability/counts. A successful HTTP response does not establish
provider accuracy, language coverage or full end-to-end conversation behavior.

Validation on 2026-10-18 (Windows): **162 combined offline checks pass** (92 core,
70 toolkit); **17 live provider checks pass** (9 headline feeds, 8 API requests).
The installed Gemini SDK also accepts the public tool schemas and response format.

Before release, record separately:

- [ ] Actual v10/Gemini spoken requests, correct source/date/unit attribution,
  city clarification, named-profile updates/strict feeds/forgetting across restart.
- [ ] Normal conversation after a tool response, multi-tool requests, interruption,
  reconnect and slow/unavailable providers on the robot.
- [ ] Disabled/group-disabled behavior in the real session and unchanged core audio/video.
- [ ] Docker build and inclusion of toolkit code without runtime preference data.
- [ ] Target-hardware CPU/network behavior and any other claimed platforms.
- [ ] Provider commercial usage/rate limits/attribution, including Open-Meteo's
  non-commercial public service and publisher RSS terms. No commercial fleet terms
  or paid market-data licenses are assumed by this experiment.
- [ ] Software-lead review and approval. Offline/provider success is not deployment approval.

Five Discovery additions (trivia, TV, poetry, radio, recipes) are documented in [CONVERSATION.md](CONVERSATION.md).
