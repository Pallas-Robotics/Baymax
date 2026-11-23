"""Safety and privacy guards for the robot rig.

- Refuse to run anywhere but a marked test robot.
- Only collaborators' same-repository PRs labelled `robot-test`, or a collaborator's
  "Run workflow" submission of one of this repository's branches, may run (the code
  runs with a Gemini key).
- Give the runtime its key through a private file, never through .env or logs.
- Give the runtime a private, empty data folder (`BAYMAX_DATA_DIR`) for the run: its
  raw audio segments, session audio, transcripts and v10 memory store go there, and
  the folder is deleted afterwards. The robot's own memories are never read or added to.
- After every run, also delete any audio/transcripts written in the checkout itself
  (runtimes that ignore BAYMAX_DATA_DIR).
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import urllib.request

MARKER = Path("/etc/ember/test-robot")
LABEL = "robot-test"
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
WRITE_PERMISSIONS = {"admin", "maintain", "write"}
ROOM_NOTICE = ("ROBOT RIG: this robot's microphone and camera stream to Google Gemini during the run. "
               "The room must be empty of people and private material; only the rig's synthetic speech, "
               "cards and noise should be audible or visible.")
# What the v8/v9/v10 runtimes write locally (BAYMAX_DATA_DIR, default Baymax-main): raw mic
# segments, merged session audio and transcripts. Only files modified since the run started are removed.
RECORDINGS = ("day_utterance/seg_*.wav", "session_*.wav", "od_session_*.wav", "transcript_*.txt")
# Settings the rig always gives the runtime, on top of the robot's device settings.
RIG_ENV = {
    "BAYMAX_VOICE_ANALYSIS": "0",          # No end-of-session voice analysis of the recording.
    "BAYMAX_AUDIO_RETENTION_DAYS": "0.01",  # Prune old segments (<= 0 would DISABLE pruning); the rig deletes the rest.
}
SECRET_NAMES = ("GOOGLE_API_KEY", "GOOGLE_API_KEY_FILE", "GEMINI_TEST_KEY", "BAYMAX_EMAIL_PASSWORD")


def is_test_robot(environ=os.environ, marker=MARKER):
    return environ.get("EMBER_TEST_ROBOT") == "1" or Path(marker).is_file()


# ── Who may trigger a run ────────────────────────────────────────────────────
def check_event(event, repository, permission=None, label=LABEL):
    """(allowed, reason) for a GitHub `pull_request` event. `permission(login)` returns the
    sender's repository permission (admin/maintain/write/read/none)."""
    pr = event.get("pull_request") or {}
    if event.get("action") != "labeled" or (event.get("label") or {}).get("name") != label:
        return False, f"not a '{label}' label event"
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    if head_repo != repository:
        return False, f"PR comes from {head_repo or 'a deleted fork'}, not {repository}; forks never run on the robot"
    if pr.get("state") != "open":
        return False, "PR is not open"
    if pr.get("author_association") not in TRUSTED_ASSOCIATIONS:
        return False, f"PR author is {pr.get('author_association')}, not a collaborator"
    sender = (event.get("sender") or {}).get("login")
    if permission is None:
        return False, "cannot check the labeller's permission"
    level = permission(sender)
    if level not in WRITE_PERMISSIONS:
        return False, f"{sender} has {level!r} permission; only collaborators with write access may label"
    return True, "ok"


# A branch of this repository only: "refs/..." could name a fork's PR head (refs/pull/N/head).
BRANCH = re.compile(r"(?!refs/)(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}")
SCENARIO_LIST = re.compile(r"[a-z0-9-]+(,[a-z0-9-]+)*")


def check_dispatch(event, repository, permission=None):
    """(allowed, reason) for a `workflow_dispatch` submission (Actions → Robot test → Run
    workflow). Inputs: `branch` (required), `scenarios` (comma-separated names) and `pr`."""
    inputs = event.get("inputs") or {}
    if (event.get("repository") or {}).get("full_name") != repository:
        return False, "submitted from another repository"
    if not BRANCH.fullmatch(inputs.get("branch") or ""):
        return False, "branch must be the name of a branch in this repository (no refs/, no '..')"
    if inputs.get("scenarios") and not SCENARIO_LIST.fullmatch(inputs["scenarios"]):
        return False, "scenarios must be comma-separated scenario names"
    if inputs.get("pr") and not str(inputs["pr"]).isdigit():
        return False, "pr must be a PR number"
    sender = (event.get("sender") or {}).get("login")
    if permission is None:
        return False, "cannot check the submitter's permission"
    level = permission(sender)
    if level not in WRITE_PERMISSIONS:
        return False, f"{sender} has {level!r} permission; only collaborators with write access may submit"
    return True, "ok"


def github_permission(repository, token, opener=urllib.request.urlopen):
    """Read-only GET of a user's permission on the repository."""
    def lookup(login):
        if not login:
            return "none"
        request = urllib.request.Request(f"https://api.github.com/repos/{repository}/collaborators/{login}/permission",
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Accept": "application/vnd.github+json"})
        try:
            with opener(request, timeout=20) as response:
                return json.loads(response.read().decode()).get("permission", "none")
        except Exception:
            return "none"
    return lookup


# ── Key and .env for the branch under test ───────────────────────────────────
def read_env_file(path):
    values = {}
    if path and Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and not name.startswith("#"):
                values[name.strip()] = value.strip().strip('"').strip("'")
    return values


class PreparedEnv:
    """Writes the branch's .env (robot devices + rig settings + key file path) and undoes it."""

    def __init__(self, root, base_env, key, extra=None, temp_root=None):
        self.root, self.base_env, self.key, self.extra = Path(root), base_env, key, extra or {}
        self.temp_root, self.folder, self.backup = temp_root, None, None
        self.data_dir, self.trace_path, self.data_removed = None, None, 0

    def __enter__(self):
        if not self.key:
            raise RuntimeError("No Gemini test key (set GEMINI_TEST_KEY).")
        values = {k: v for k, v in read_env_file(self.base_env).items() if k not in SECRET_NAMES}
        values.update(RIG_ENV)
        values.update(self.extra)
        self.folder = Path(tempfile.mkdtemp(prefix="ember-rig-key-", dir=self.temp_root))
        os.chmod(self.folder, 0o700)
        key_file = self.folder / "key"
        key_file.write_text(self.key, encoding="utf-8")
        os.chmod(key_file, 0o600)
        values["GOOGLE_API_KEY_FILE"] = str(key_file)
        # A private data folder: recordings, transcripts and memories of the run, deleted afterwards.
        self.data_dir = Path(tempfile.mkdtemp(prefix="ember-rig-data-", dir=self.temp_root))
        os.chmod(self.data_dir, 0o700)
        values["BAYMAX_DATA_DIR"] = str(self.data_dir)
        # The agent harness trace, when the branch's runtime supports EMBER_TRACE (ignored otherwise).
        self.trace_path = self.data_dir / "trace.jsonl"
        values.setdefault("EMBER_TRACE", str(self.trace_path))
        self.trace_path = Path(values["EMBER_TRACE"])
        env_path = self.root / ".env"
        if env_path.exists():
            self.backup = env_path.with_name(".env.rig-backup")
            shutil.copy2(env_path, self.backup)
        env_path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        os.chmod(env_path, 0o600)
        self.values = values
        return self

    def __exit__(self, *exc):
        env_path = self.root / ".env"
        if self.backup is not None and self.backup.exists():
            os.replace(self.backup, env_path)
        else:
            env_path.unlink(missing_ok=True)
        if self.folder is not None:
            shutil.rmtree(self.folder, ignore_errors=True)
        if self.data_dir is not None and self.data_dir.exists():
            self.data_removed = sum(1 for pattern in RECORDINGS for _ in self.data_dir.glob(pattern))
            shutil.rmtree(self.data_dir, ignore_errors=True)
        return False


def ember_speaker_names(values):
    return [values.get("BAYMAX_SPEAKER_NAME", ""), "UACDemo", "Jieli"]


# ── Recordings ───────────────────────────────────────────────────────────────
def delete_recordings(root, since, patterns=RECORDINGS):
    """Remove runtime recordings/transcripts written since `since`. Returns the count per pattern."""
    removed = {}
    for pattern in patterns:
        count = 0
        for path in Path(root).glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime >= since - 5:
                    path.unlink()
                    count += 1
            except OSError:
                continue
        removed[pattern] = count
    return removed


def leftover_recordings(root, since, patterns=RECORDINGS):
    return [str(p) for pattern in patterns for p in Path(root).glob(pattern)
            if p.is_file() and p.stat().st_mtime >= since - 5]


def main(argv=None, opener=urllib.request.urlopen):
    """GitHub Actions guard step: exit 0 only for a collaborator's same-repo `robot-test` label
    or a collaborator's branch submission."""
    parser = argparse.ArgumentParser(description="Robot-test trigger guard")
    parser.add_argument("--event", type=Path, default=os.getenv("GITHUB_EVENT_PATH"))
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY"))
    parser.add_argument("--event-name", default=os.getenv("GITHUB_EVENT_NAME", "pull_request"))
    args = parser.parse_args(argv)
    if not is_test_robot():
        print("Refusing: this machine has no /etc/ember/test-robot marker.", file=sys.stderr)
        return 1
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    token = os.getenv("GITHUB_TOKEN", "")
    check = check_dispatch if args.event_name == "workflow_dispatch" else check_event
    allowed, reason = check(event, args.repository,
                            github_permission(args.repository, token, opener) if token else None)
    print(f"robot-test guard: {'allowed' if allowed else 'refused'} ({reason}) at {time.strftime('%H:%M:%S')}")
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
