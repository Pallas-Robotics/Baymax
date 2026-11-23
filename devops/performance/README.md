# Feature performance check

Run on the Linux Orange Pi with Ember already running. This read-only sampler
measures system CPU, runtime **and descendant** CPU/RSS, system RAM used, the
hottest exposed thermal zone, exposed NPU load, and existing speech-to-transcript,
reply-audio and scheduled-playback latency. It creates no artificial load and
opens no audio/camera streams or provider connections. Exercise the feature
yourself or with its existing approved scenario during collection.

## Baseline, feature, then combined load

Use the same board, OS, runtime build, peripherals, provider/model, network,
sample duration and repeatable utterances. Record feature settings and workload
with the PR. Warm up first, and let temperature settle between comparisons.
Change only the feature under test. From the repository root:

```bash
# Feature off, with the SAME conversation workload (not silence for a latency comparison).
python devops/performance/check.py --feature baseline --seconds 120 --min-turns 10 --output .local-review/performance/baseline.json

# Enable the feature through its normal configuration, warm up, repeat those ten turns.
python devops/performance/check.py --feature eyes --seconds 120 --min-turns 10 --baseline .local-review/performance/baseline.json --max-added-latency-ms 250 --output .local-review/performance/eyes.json
```

`250 ms` is an example review budget, not a certified limit. Agree on budgets
for the board/workload and supply them explicitly. Optional peak limits:
`--max-cpu-percent 85 --max-rss-mib 1500 --max-temp-c 80 --max-audible-ms 3000`.
Include all required limits for a release check. The report compares mean/p95/max
for every metric available in both runs. `--max-added-latency-ms` gates the
increase in **p95 speech-end-to-playback** relative to the baseline. Repeat runs
to distinguish provider/network variation from a feature regression.

For a resource-only idle baseline, omit `--min-turns`; latency will be unavailable
and cannot pass a latency budget. Afterwards run the normal full feature mix
using the same command with a label such as `conversation+pose+eyes`. For startup,
use an explicit current `--pid` and collect a separate cold-start window; the
normal warmed scenario does not measure boot time.

## Results and interpretation

- JSON contains aggregate counts/mean/p95/peak, baseline deltas, board/kernel,
  sample duration and failures, never transcript text or raw status snapshots.
  Keep reports under ignored `.local-review/`; review before sharing.
- Exit 0: measurements collected and supplied requirements passed. `MEASURED`
  means no budgets were supplied; it does not mean performance was approved.
  `PASS` applies only to supplied requirements. Exit 1: unmet requirement.
  Exit 2: unsupported OS, missing runtime, invalid input or collection error.
- Missing sensors are `null`/unavailable, never zero load. A required missing
  measurement fails. NPU load uses readable vendor `rknpu*/load` (RK3588) or
  `viplite/core_loading` (observed on the 4 Pro A733) debugfs files, taking the
  highest reported core percentage. Access may require a privileged read;
  `--npu-load-file /path/to/vendor/load` supports another percentage-format file.
  `--require-npu` requires telemetry; `--max-npu-percent` adds a load budget.
  Do not change driver permissions/mount debugfs as a side effect of this check.
  This does not prove NPU inference correctness or measure NPU memory.
- CPU system percentage is 0–100 across the machine. Process CPU is core-based
  and can exceed 100%. RSS sums parent/descendants and can double-count shared
  pages; it is not PSS. CPU for a newly seen child starts at its next sample;
  very short processes between samples can be missed. System RAM includes other
  applications and the sampler. Missing thermal data cannot establish safety;
  a sampled peak cannot rule out brief spikes or prove absence of throttling.
- Timing reuses local speech estimates and scheduled DAC playback, not Gemini
  internals or acoustic measurements. Only speech turns ending during the window
  are included, with repeated snapshots deduplicated. Leave time after the last
  utterance for playback. Missing/stale monitor data fails a requested latency
  check. Check expected utterance count and manually record unrecognized speech;
  speech that never becomes a transcript may have no matched timing row.
- The sampler is not a stress generator or automatic feature toggle. Use bounded
  realistic scenarios, then the full normal feature mix. Stop the scenario if
  hardware becomes unstable; do not deliberately overheat the board.

## Future feature PRs

Each feature README/PR must name its idle/baseline and active scenario, sample
duration, expected speech turns, board-specific budgets and off/on/combined
results. Explain unavailable metrics and NPU N/A versus required telemetry.
Include CPU/RAM peaks, temperature peak and added p95 latency. Core offline tests
check the sampler logic; real readings remain a separate hardware validation:

| Feature | Repeatable active scenario (alongside the same conversation turns) |
|---|---|
| Conversation | Fixed utterances with quiet gaps; record missed/duplicate replies |
| Eyes | Normal listening/thinking/speaking animations and lifecycle |
| Pose | Same camera scene/resolution/backend; normal inference while speaking |
| Memory | Same prepared local fixture and retrieval/save prompts |
| Toolkits | Same approved calls; include completion, failure/timeout and stop behavior |
| Setup/startup | Separate cold-start/restart window, then normal warmed workload |
| Any new feature | Document its bounded scenario and exact enabled/off settings |

Copy this result table into each feature PR (N/A requires a reason):

| Metric | Baseline | Feature active | Combined load | Budget / result |
|---|---|---|---|---|
| System CPU peak % / runtime tree CPU peak % | | | | |
| Runtime tree RSS peak MiB / system RAM used MiB | | | | |
| NPU peak % / telemetry availability | | | | |
| Hottest exposed temperature peak C | | | | |
| Recognition / reply / playback p95 ms | | | | |
| Added playback p95 ms / completed and missed turns | | | | |

```bash
python tests/run.py --feature performance
```
