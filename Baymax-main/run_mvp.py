"""MVP: configure explicit devices once, then start the integrated v8 runtime."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
from dotenv import load_dotenv

from dev_settings import public_configuration
from pc_hardware import use_performance_cpus
from runtime_monitor import read_status
from features.eyes.controller import eyes_session

ROOT = Path(__file__).resolve().parent


def configuration_ready(path):
    config = public_configuration(path)
    return config["key_configured"] and all(
        config[field] != "" for field in ("microphone", "speaker", "camera")
    )


def pid_alive(pid):
    """True/False on POSIX; None when unknown (Windows os.kill would terminate the process)."""
    if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runtime_running(status, alive=None):
    # A killed runtime leaves a status heartbeat that looks live for ~5 s; a quick
    # restart must not mistake that for a running Ember and exit as "success".
    if status.get("state") in ("offline", "stopped"):
        return False
    return (alive or pid_alive)(status.get("pid")) is not False


@contextmanager
def runtime_lock(path):
    # OS locks release after a crash, unlike a persistent PID/sentinel file.
    with path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("MVP is already starting or running.") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def configure_once():
    print("First run: choose microphone, speaker and camera (or No camera), then save. "
          "Ember starts after saving and sends microphone/camera data to Gemini; "
          "session audio is recorded locally.", flush=True)
    setup = subprocess.Popen([sys.executable, str(ROOT / "devUI.py"), "--setup-once", "--port", "8767"], cwd=ROOT)
    try:
        return setup.wait() == 0 and configuration_ready(ROOT / ".env")
    finally:
        if setup.poll() is None:
            setup.terminate()
            try:
                setup.wait(timeout=5)
            except subprocess.TimeoutExpired:
                setup.kill()
                setup.wait()


def _stop(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="Explicitly reopen setup.")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _stop)
    try:
        with runtime_lock(ROOT / ".mvp.lock"):
            if runtime_running(read_status(ROOT / ".runtime-status.json")):
                print("Ember is already running. Stop it before starting MVP.")
                return 0
            # Saved eye overrides apply before the eyes start; confused eyes then
            # cover Setup, heavy imports and model loading until Gemini connects.
            load_dotenv(ROOT / ".env", override=True, interpolate=False)
            # Before the eyes/network threads and numpy's OpenBLAS pool start, so they run
            # on the fast cores too (by default the RK3588 only; run_pc's call is then a no-op).
            use_performance_cpus()
            with eyes_session() as eyes:
                if args.setup or not configuration_ready(ROOT / ".env"):
                    if not configure_once():
                        print("Setup was not completed; Ember was not started.")
                        return 0
                # Existing launcher owns credential/model preflight and audio lifecycle.
                # Runtime failures never reopen setup or silently switch saved devices.
                load_dotenv(ROOT / ".env", override=True, interpolate=False)
                from run_pc import main as start_runtime
                os.environ.setdefault("BAYMAX_WAIT_CUE_ENABLED", "0")  # The fixed "hmm" felt unsettling.
                os.environ.setdefault("BAYMAX_INTERRUPTION_ENABLED", "0")
                # With interruptions off the mic is muted while Ember speaks; Gemini gets
                # room-level noise then, not digital zeros, which held up its next turn.
                os.environ.setdefault("BAYMAX_COMFORT_NOISE", "1")
                # Who is in front of Ember (face) and who is talking (voice), models in their
                # own processes on the slow cores (features/person_recognition/README.md).
                os.environ.setdefault("BAYMAX_PEOPLE_RECOGNITION", "1")
                os.environ.setdefault("BAYMAX_TURN_END_SILENCE_MS", "400")
                os.environ.setdefault("BAYMAX_VOICE_ANALYSIS", "0")
                # The service stops in 15 s (TimeoutStopSec); memory gets 10 of them.
                os.environ.setdefault("BAYMAX_MEMORY_STOP_SECONDS", "10")
                code = start_runtime()
                if code and eyes is not None:
                    eyes.exit_failed = True  # Confused while systemd retries; X after repeated failures.
                return code
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as error:
        print(f"MVP startup stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
