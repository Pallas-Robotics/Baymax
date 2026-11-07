# Editing and validation protocol

For people and LLMs changing Ember. Read this file, the repository `AGENTS.md`,
the relevant feature README and [feature log](../docs/features/FEATURE_LOG.md)
before editing. This protocol is a workflow, not permission to publish, merge,
deploy, contact recipients or start hardware capture without task authorization.

## 1. Establish the right scope

- Orange Pi work belongs in `Pallas-Robotics/Baymax`. Check `git remote -v`,
  branch and working-tree status. LattePanda's old PR #20 is a separate line.
- Start a focused branch from `main`. `release` is the robot update channel;
  do not move it as a side effect of development.
- Preserve existing user edits. Read the callers/tests for the code being changed.
- Use the feature folders. Keep existing startup/import/data paths working; a
  broad reorganization needs its own reviewed migration.
- Preserve prompts, model selection, sample rates and data flows unless the task
  explicitly changes them. Do not add extra microphone/camera streams for tools.

## 2. Make a focused change

- Core features: `Baymax-main/features/`; toolkit groups are children of `toolkits/`.
- Shared runtime, device setup and lifecycle: existing `Baymax-main/` entry points.
- SD-image tooling: `devops/opi4-flash/`; graph tooling: `devops/graphify/`.
- Keep credentials, private settings, recordings, model downloads, runtime
  databases, personalized images and machine-specific logs out of Git and graphs.
- Add a meaningful regression check for behavior changes. Reuse existing test
  helpers; do not duplicate a feature's implementation in its test.

## 3. Select the tests

The root `tests/` tree mirrors feature ownership. Its suite definitions reuse
the existing application/deployment tests, keeping their established paths.

```bash
python tests/run.py --list
python tests/run.py --feature conversational_ability
python tests/run.py --feature eyes --feature pose
python tests/run.py --changed origin/main
python tests/run.py
```

- A feature command runs that feature **and overall checks**.
- `--changed` includes committed, staged, unstaged and untracked changes relative
  to the named ref. Shared-runtime/dependency/test-policy changes select all
  suites. Unrecognized source changes also select all, rather than miss coverage.
- No selection means all suites. Full validation is required for shared changes
  and before declaring a release ready; targeted checks accelerate local work.
- Application tests retain `Baymax-main/check_release.py` as the robot/CI gate.
  That gate also discovers root overall checks, so source parsing, test ownership
  and the chat UI check participate in existing CI without a workflow change.
- Existing CI filters miss root-only tests/docs/devops changes. Enable the full
  workflow described in `tests/README.md`; until then run the root gate manually
  and include its report. Do not claim automatic coverage that is not enabled.
- Node is needed for the chat UI check. Local machines without it record a skip;
  CI must have it. Explain every skip, failure or unavailable platform.
- Every new `Baymax-main/tests/test_*.py` file must belong to exactly one suite.
  Update the matching `tests/**/suite.json`; the overall gate enforces coverage.

Reports/logs go to `.local-review/validation/`. Do not paste private sessions or
credentials into a PR. A successful import, empty test suite, active service or
green offline run is not proof of physical operation.

## 4. Update documentation and Graphify

- Update `CHANGELOG.md`, the feature README and the feature log for user-visible
  behavior, settings, dependencies, external data flows or compatibility changes.
- Describe **implemented**, **reserved**, **offline checked** and **live pending**
  separately. Never infer all-board support from a CPU/NPU fallback or an import.
- Refresh Graphify's semantic snapshot when documented relationships change,
  then run `python devops/graphify/build.py`. See
  [the Graphify guide](../docs/features/GRAPHIFY.md) for optional tool dependencies.
- Keep source references relative and valid. Review graph omissions/confidence;
  a graph is navigation, not release evidence. Do not publish internal cache paths.

## 5. Review the PR

- Run `git diff --check` and inspect the complete staged diff for unrelated work
  and secrets. Commit only the intended source, tests and public documentation.
- Use a clear title and the repository PR template. State the problem/result,
  affected features, actual commands/results, board/OS, skips and live checks left.
- Do not add generated-by or assistant coauthor notes. Preserve legal notices and
  the existing Git history; rewriting shared history is a separate operation.
- Existing CI checks must be reviewed by name/platform. Report failures honestly;
  do not treat an unrelated successful job as a passing PR.
- Use a draft PR while required validation is pending. The software lead reviews
  evidence before merge/deployment. A test runner cannot grant that approval.

## 6. Hardware and release validation

- Follow `Baymax-main/TESTING_PROTOCOL.md` for core/toolkit live checks. Identify
  the board, peripherals and data sent to providers before starting a live test.
- Test audio/camera selection, actual speech/replies, concurrent pose load,
  stop/restart, reconnect recovery, and any changed feature on the target board.
- Every feature PR includes the [shared performance check](performance/README.md):
  repeatable baseline/off, active/on and normal combined-load scenarios. Record
  CPU, parent/child RAM, NPU load/availability, temperature and recognition/reply/
  playback latency. Compare added p95 latency and resource deltas against explicit
  board/workload budgets. Report unavailable sensors and misses; do not call a
  metrics-only run or absent NPU telemetry a passing hardware test. Feature test
  commands always include the sampler's offline regression checks; actual
  performance requires the separate live sampler on the target Linux board.
- A fresh-card build/write/boot is separate from source tests. The 4 Pro image
  does not support the 5 Max or every Orange Pi model.
- Update/promote the approved runtime through the documented release workflow.
  Keep rollback evidence. Do not enable production alerts as a test shortcut.
