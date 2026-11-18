"""Validate Gemini access, then run the version-10 entry point with PC hardware."""
import os
from pathlib import Path
import runpy
import sys
from dotenv import load_dotenv
from pc_hardware import use_performance_cpus
from secret_config import CredentialError, gemini_key
from gemini_preflight import PreflightError, validate_key
from features.conversational_ability import live_models

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "realtime_gemini_10.py"
EMAIL_SETTINGS = ("BAYMAX_EMAIL_FROM", "BAYMAX_EMAIL_PASSWORD", "BAYMAX_EMAIL_TO")


def main():
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env", override=True, interpolate=False)
    # First (after .env, for BAYMAX_CPU_PINNING), so every thread starts on the fast
    # cores. By default only on the Orange Pi 5 (RK3588); elsewhere it does nothing.
    use_performance_cpus()
    os.environ["BAYMAX_HARDWARE_PROFILE"] = "pc"
    os.environ.setdefault("BAYMAX_AUDIO_ENHANCEMENT", "1")
    try:
        key = gemini_key()
    except CredentialError as error:
        print(f"Startup stopped: {error}", file=sys.stderr)
        return 1
    if not key:
        print("Open Setup, paste your Gemini key, and Save configuration. Then start again. Do not paste the key into chat.", file=sys.stderr)
        return 1
    if any(os.getenv(name) for name in EMAIL_SETTINGS):
        print("Email credentials detected. Remove them from this development config/environment before starting.", file=sys.stderr)
        return 1
    print("Checking Gemini key and configured model access with Google (no audio/video sent)...", flush=True)
    try:
        # v10 connects with the first of its Live models (BAYMAX_LIVE_MODELS) this key can use.
        validate_key(key, RUNTIME, live_models=live_models.configured)
    except PreflightError as error:
        print(f"Startup stopped: {error}", file=sys.stderr)
        return 1
    print("Google accepted the key; configured models are accessible. Live-session quota/access will be checked when connecting.", flush=True)
    print("Starting Gemini Live version 10 with the configured audio devices.", flush=True)
    print("Microphone audio and camera frames are sent to Gemini; upstream also records session audio locally.", flush=True)
    os.environ["GOOGLE_API_KEY"] = key  # Process-local only; never copy a key file into .env.
    runpy.run_path(str(RUNTIME), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
