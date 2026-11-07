# Repository tests

One entry point, with overall checks and feature-owned suites:

```bash
python tests/run.py                          # all suites
python tests/run.py --feature eyes           # eyes + overall
python tests/run.py --feature conversational_ability
python tests/run.py --feature sd_card_flash
python tests/run.py --feature languages       # offline multilingual harness checks
python tests/run.py --changed origin/main     # changed features + overall
python tests/run.py --list
```

Run from the repository root using an environment with the application's test
dependencies installed. Node enables the chat UI check. Reports and complete logs
are under `.local-review/validation/`; a failure returns a nonzero exit status.

```text
tests/
  overall/                       Shared gate, source syntax, ownership and UI checks
  features/
    startup/ device_settings/ runtime/
    conversational_ability/
    eyes/
    pose/
    memory_palace/
    person_recognition/           Reserved namespace; no claimed identity-engine tests
    toolkits/{everyday,family,media,information}/
  documentation/graphify/
  deployment/sd_card_flash/
  performance/                   Shared CPU/RAM/NPU/thermal/latency sampler tests
```

`performance/` owns the shared sampler regression checks. Every feature/changed
selection includes these and overall checks. The separate
[hardware performance protocol](../devops/performance/README.md) measures CPU,
RAM, NPU, temperature and added latency under baseline/feature/combined load;
offline tests do not invent readings or launch a live scenario.

Each folder's `suite.json` owns source patterns and references its test files.
Existing test modules stay in `Baymax-main/tests` and `devops` so imports and robot
updaters keep working. New application tests must be assigned to one feature;
overall checks reject missing or duplicate ownership. Suites run in separate
processes to isolate imports/settings and same-named deployment test modules.

Shared entry points, dependencies, CI/test-policy changes and unknown source
paths cause `--changed` to choose all suites. Reserved features are marked N/A,
not presented as an implemented feature with passing tests. Feature selection is
for fast development; a focused pass is not full release validation.

`Baymax-main/check_release.py` remains the existing core/toolkit gate used by
robots and CI and also runs root overall checks. Local skips are reported; the
chat UI check requires Node in CI. Physical devices and provider calls are outside
these offline suites. Follow [the editing protocol](../devops/EDITING_PROTOCOL.md)
and the live checklist before release.

## CI activation

The existing workflow filters for `Baymax-main/**`; changes confined to root
tests, documentation or deployment tools do not trigger that workflow. The ready
workflow at `devops/ci/feature-validation.yml.example` covers every PR and pushes
to `main`/`release`, runs all suites and uploads the report/logs. A maintainer must
copy it to `.github/workflows/feature-validation.yml` using a credential with
workflow-write access. The current publishing credential lacks that scope.
Until enabled, run `python tests/run.py` explicitly for those changes and attach
the results. This template has not run in Actions yet.
