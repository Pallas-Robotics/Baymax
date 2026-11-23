# Conversation discovery tools

On by default (trivia, brain games, TV shows, poems, radio stations, recipes, plus Wikipedia
and Open Library), independently of the opt-in public toolkit, so saving Setup
with public information off keeps them. `EMBER_DISCOVERY_ENABLED=0` turns them
off; `BAYMAX_TOOLKIT_ENABLED=0` disables these along with the other public tools.
No new dependencies, microphone/camera streams, scheduled jobs or startup requests.
Results are fetched on demand and cached in memory; only requested results enter
Gemini's conversation context. Provider text is untrusted data, not instructions.

| Tool | Input sent to provider | Output and cache |
| --- | --- | --- |
| `public_trivia` / [Open Trivia DB](https://opentdb.com/api_config.php) | Five-question batch request | One decoded question/answer per call; remaining questions stay local until consumed or restart. Five-second local refill cooldown; provider IP limits also apply. CC BY-SA 4.0. |
| `public_shows` / [TVmaze](https://www.tvmaze.com/api) | Title query, then unique matching show ID | Up to three titles/summaries; cast and next episode for a single match. Clarify multiple matches. One-hour cache; absent next episode means unknown. CC BY-SA attribution required. |
| `public_poem` / [PoetryDB](https://github.com/thundercomb/poetrydb) | Optional author, otherwise random request | One poem, at most 16 lines, labelled excerpt when truncated. One-hour cache; repeat calls can return the same poem. |
| `public_radio` / [Radio Browser](https://api.radio-browser.info/) | Genre and optional two-letter station country | Up to five station descriptions/homepages; one-hour cache. Discovery only: stream URLs are not returned to Gemini. Playback is the separate `listen` tool (see README). Fixed audited de1 mirror; outages return unavailable. |
| `brain_game` (local, no provider) | Nothing leaves the robot | Start/next/result/score/end for word association, twenty questions, categories, list recall, story recall, riddles and word puzzles. Rounds come from local English lists without repeats until a list is used up; the score lasts until the game ends or Ember restarts. See [brain games](general_media_entertainment/games/README.md). |
| `public_meals` / [TheMealDB](https://www.themealdb.com/api.php) | Meal-name query | Up to three recipes with ingredients/instructions; one-hour cache. Development/education key 1; public app-store release needs supporter access before shipping. No allergy guarantees. |

Queries go to the named third-party providers. No identity, saved profile,
recordings or inferred location are sent. Queries and results remain in a bounded
process-local cache; restart clears them. The existing transport enforces HTTPS,
allowlisted hosts, no redirects, five-second socket timeout and a 2 MB response cap.
The tool runner bounds each call by its limit in `time_limits.py` (12 s for these lookups) and eight calls per batch.
Failures return unavailable; never fabricate results or use expired fallback data.
Trivia questions can repeat across batches; there is no persistent quiz history.

## Verification

From `Baymax-main`, run:

```
python -m unittest discover -s tests -p "test_toolkit_conversation.py"
python -m unittest discover -s tests
python check_release.py --report .local-review/conversation-tools.md
```

Offline tests cover all five adapters, malformed provider results, validation,
caching, trivia batch consumption/cooldown, attribution,
show ambiguity, registry membership and disabled behavior. Existing public-tool
integration tests cover configuration and asynchronous Gemini response routing.

Live checks pending: each provider's normal/no-result/rate-limit behavior; spoken
requests and follow-up conversation through Gemini; trivia answer timing; show
ambiguity; source attribution; disabling Discovery in Setup. Run deliberately and
record OS/runtime/date and results using TESTING_PROTOCOL.md; offline passing is
not deployment approval. No live capture is started by these tests.

## Current implementation evidence (2026-10-21)

- Offline gate: core 220/220; toolkit 113/113; no skips. Includes 10 new adapter tests.
- Live generic requests: trivia, TV title search, random poem, jazz radio search,
  and Arrabiata recipe search passed. Cast/schedule currently have mocked coverage only.
- Spoken Gemini and device checks remain pending. No capture started.
- Folder layout: `general_media_entertainment/games/trivia.py` contains the game;
  `general_media_entertainment/sources.py` contains TV, poetry and radio adapters.
  Recipes remain in `conversation_sources.py`.
