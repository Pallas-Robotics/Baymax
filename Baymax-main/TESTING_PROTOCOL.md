# Ember testing and review protocol

Every feature PR must verify both the core runtime and the relevant toolkits. Authors test and provide evidence; the software lead reviews the results and decides whether to approve. An offline pass is not deployment approval.

## 1. Run the offline gate

From `Baymax-main`, use the installed environment interpreter:

```powershell
# Windows
.\.venv\Scripts\python.exe check_release.py --report .local-review/report.md
```

```bash
# macOS/Linux
.venv/bin/python check_release.py --report .local-review/report.md
```

This runs all `tests/test_*.py`, prints separate core and toolkit results, and generates a fresh manual checklist. Exit code 0 requires both sections to contain tests and pass; failures or missing required test files return 1. No API key, capture or live service is needed. The checks test the shipped defaults: while they run, this machine's `BAYMAX_*`/`EMBER_*` settings and API keys are out of the environment and its `.env` reads as empty, so a robot's own settings cannot fail them (a test that needs a setting sets it). Reports overwrite the chosen path, so save completed evidence separately before rerunning. `.local-review/` is ignored by Git.

Core checks cover configuration, UI protections, hardware selection, upstream runtime parity and monitor behavior. Toolkit checks cover mocked location/weather behavior, caching, ambiguity, failures and Gemini tool-response routing. The runner also has regression tests for its failure/report behavior. These tests were added for this desktop implementation; they are not a full Baymax suite.

## 1b. Optional: Gemini-in-the-loop checks before the robot

`python evals/live_eval.py --dry-run`, then without `--dry-run`, plays synthetic speech and pictures into Ember's real MVP Gemini session and checks transcripts, tool calls, replies and reply latency over several runs (`evals/README.md`). It needs `GOOGLE_API_KEY` and sends only the listed synthetic data. Paste the summary table into the PR as Gemini-behaviour evidence; it does not replace the robot checks below.

## 1c. Optional: the test robot (Layer 2)

A collaborator can add the `robot-test` label to a same-repository PR (or submit a branch through Actions → Robot test → Run workflow). A dedicated test robot then runs the PR's real runtime against a scripted synthetic "person" on a second speaker and posts a report (`evals/robot/README.md`). The report is hardware evidence for the reviewer. It does not replace the human checks below that it lists as needing a person, such as eye appearance and real voices, and it is not approval. Until the test robot and its runner are set up, this layer is not available.

## 2. Complete live core checks

Use the report's core checklist: fresh setup, startup key/model verification (a rejected key must stop before capture), actual device tests, spoken Gemini conversation, multiple turns, interruption, live monitor, stop/restart and core conversation with toolkits disabled. Run these deliberately: `run_pc.py` sends microphone/camera data to Gemini and the upstream runtime records audio locally. Never let the offline CLI start capture automatically.

Record what was tested, actual results, commit, date, OS, devices and runtime baseline. Record failures as failures and untested items as pending. Only mark N/A with a reason. Check platform/deployed-hardware claims on those actual targets; Windows success does not establish macOS/Linux or robot compatibility.

## 3. Complete toolkit and integration checks

Use the report's location/weather checklist, including city correction, ambiguity, units, provider accuracy/time, other-city weather, grouped monitor events and conversation after a tool call. Exercise slow/unavailable services. Mocked failures in the offline suite do not prove live recovery.

Each added or changed toolkit needs its own README, offline tests named `tests/test_toolkit*.py`, and PR checklist covering normal inputs, invalid/ambiguous inputs, failures/timeouts, core integration and disabled behavior. Describe dependencies and external data flows. New tests are discovered automatically; maintaining meaningful coverage is the author's and reviewer's responsibility.

## 4. Submit evidence for review

Use the PR template and update `CHANGELOG.md`. Include separate core and toolkit results and a sanitized copy of the completed checklist. Do not include keys, private transcripts or recordings. James reviews pending checks and any exceptions before approving. Repository checks/branch protection are separate administrative configuration; this CLI does not enforce GitHub merge permissions.
