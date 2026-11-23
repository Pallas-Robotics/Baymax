"""Draw one static face on the LED eyes and exit.

systemd runs `show.py --after-exit` as ExecStopPost on ember-mvp.service. Ember
draws its own exit face when Python shuts down (blank, confused or X); a crash
that kills the process outright (signal, core dump, stop timeout) cannot, so it
gets X eyes ("unplug and replug") here. `show.py <expression>` draws any face.
"""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from features.eyes.controller import PORT  # noqa: E402
from features.eyes.driver import open_leds  # noqa: E402
from features.eyes.patterns import EXPRESSIONS  # noqa: E402


def crashed(env):
    # systemd sets these for ExecStopPost; a clean stop is SERVICE_RESULT=success.
    return (env.get("EXIT_CODE") in ("killed", "dumped")
            or env.get("SERVICE_RESULT") in ("signal", "core-dump", "timeout", "watchdog"))


def show(face, port, board_factory=None):
    leds, close = open_leds(port, board_factory)
    try:
        for device in (0, 1):
            leds.set_intensity(device, 3)
            leds.shutdown(device, False)
        leds.draw_pair(*EXPRESSIONS[face][0][0])  # The MAX7219s keep it after exit.
    finally:
        close()


def settings():
    values = dict(os.environ)
    try:
        from dotenv import dotenv_values
        values.update({k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None})
    except Exception:
        pass
    return values


def main(argv=None, env=None, board_factory=None):
    argv = sys.argv[1:] if argv is None else argv
    env = settings() if env is None else env
    if env.get("BAYMAX_EYES_ENABLED", "1" if sys.platform.startswith("linux") else "0") != "1":
        return 0
    if argv == ["--after-exit"]:
        if not crashed(env):
            return 0
        face = "dead"
    elif len(argv) == 1 and argv[0] in EXPRESSIONS:
        face = argv[0]
    else:
        print("Usage: show.py --after-exit | " + " | ".join(EXPRESSIONS), file=sys.stderr)
        return 2
    try:
        show(face, env.get("BAYMAX_EYES_PORT") or PORT, board_factory)
        print(f"[EYES] Showing {face}.", flush=True)
    except Exception as error:
        print(f"[EYES] Could not show {face}: {error}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
