"""
Gemini Live API - Realtime S2S + Video + Fall Detection
Optimised for low latency:
- Callback-based output stream (zero event loop blocking).
- Batch-drain mic queue (eliminates mic_queue_wait buildup).
- Interrupt flushes playback buffer immediately.
- Graceful camera fallback if hardware unavailable.
- first_audio_latency tracks perceived delay (server thinking time).
- No client-side VAD — Gemini handles voice activity detection.

Added features:
- Gemini built-in input/output audio transcription (no local ASR model needed).
- Memory retrieval: on each user turn, retrieves semantically similar memories
  from ChromaDB and injects them as context via send_client_content.
- During uptime (every MEMORY_SAVE_USER_TURNS user turns, or after a quiet spell)
  and on Ctrl+C: Gemini Flash summarises the conversation since the last save and
  the summaries are embedded into ChromaDB.
- Fall detection: MediaPipe Pose runs in a dedicated thread, shares camera with
  Gemini video sender. On fall detection, alerts Gemini via the live session.
  FallDetector is vendored in fall_detection/ (was /home/meowmax/fall_detection).
- Conversation initiation (v9): after a stretch of robot silence the robot enters
  "initiation mode"; while the user's face is visible a per-tick coin (derived
  from an authored sigmoid CDF F(t)) decides when the robot opens a conversation
  on its own, gated so it never talks over the user.

v10 (fleet AI core) = v9 with:
- Fall detection re-enabled (v9 disabled it because the detector never fired
  in practice; thresholds are unchanged and still need tuning).
- v8's pin_pipewire_sink: the default PipeWire sink is pinned to the USB speaker.
- The spectral denoiser commented out (it was already off by default in v9).
"""
import asyncio
import glob
import math
import os
import random
import stat
import subprocess
import sys
import time
import threading
import collections
import urllib.request
import uuid
import queue
import wave
import requests as http_requests
import cv2
import sounddevice as sd
import numpy as np
# NOISE SUPPRESSION DISABLED (v10) — only the denoiser below used scipy.
# from scipy.signal import lfilter as scipy_lfilter
from google import genai
from google.genai import types
import runtime_monitor as monitor
from features.toolkits.fall_detection import worker as fall_worker
from dotenv import load_dotenv
from semantic_embedder import SemanticEmbedder, download_model, missing_packages, model_present


from fall_detection.detector import FallDetector
import device_config
from features import persona
# MVP (#15) toolkits: location/weather, everyday tasks, audiobooks, eyes, public tools.
from features.toolkits import gemini_tools as public_toolkit

# Load API Key
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

# ─── Web app endpoint ────────────────────────────────────────────────────────
# Set BAYMAX_CLOUD_URL + BAYMAX_DEVICE_KEY in .env to upload to the website.
# With no device key configured, telemetry falls back to the local Flask app so
# baymax_app.py keeps working unchanged for offline/bench use. Transcripts
# never leave the robot: they always go to the local dashboard.
WEBAPP_URL = os.getenv("BAYMAX_LOCAL_URL", "http://localhost:5000")

try:
    import baymax_cloud
    _CLOUD_ENABLED = bool(baymax_cloud.DEVICE_KEY)
except ImportError:
    baymax_cloud = None
    _CLOUD_ENABLED = False


def _notify_webapp(endpoint: str, data: dict):
    """
    Report an event to the dashboard.

    Cloud mode queues to disk and retries, so a fall is not lost to a WiFi
    blip. Local mode keeps the original fire-and-forget behaviour.
    """
    if _CLOUD_ENABLED and endpoint not in baymax_cloud.ROBOT_ONLY:
        baymax_cloud.notify(endpoint, data)
        return

    def _post():
        try:
            http_requests.post(f"{WEBAPP_URL}{endpoint}", json=data, timeout=2)
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()

# ─── Resolve script directory for all relative paths ─────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Runtime data (logs, memory, accounts, audio). Defaults to next to the code;
# the container sets BAYMAX_DATA_DIR=/data, mounted from /var/lib/ember.
DATA_DIR = os.getenv("BAYMAX_DATA_DIR", SCRIPT_DIR)
CHROMA_DIR = os.path.join(DATA_DIR, "chroma_store")
ONNX_MODEL_DIR = os.path.join(SCRIPT_DIR, "all-MiniLM-L6-v2-onnx")
TRANSCRIPT_DIR = DATA_DIR

# Ensure chroma_store exists and is writable
os.makedirs(CHROMA_DIR, exist_ok=True)
try:
    os.chmod(CHROMA_DIR, stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)
except OSError:
    pass  # Best effort

# ─── Pose landmarker model ──────────────────────────────────────────────────
# "lite" (default) costs far less CPU than "full"; device.toml camera.pose_model picks it.
_POSE_MODEL = device_config.get("camera.pose_model")
if _POSE_MODEL not in ("lite", "full", "heavy"):  # It becomes part of a download URL.
    print(f"[CONFIG] Unknown camera.pose_model {_POSE_MODEL!r}; using lite.")
    _POSE_MODEL = "lite"
_POSE_MODEL_NAME = "pose_landmarker_" + _POSE_MODEL
_POSE_MODEL_PATH = os.path.join(SCRIPT_DIR, _POSE_MODEL_NAME + ".task")
_POSE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    f"pose_landmarker/{_POSE_MODEL_NAME}/float16/latest/{_POSE_MODEL_NAME}.task"
)

def _ensure_pose_model():
    if not os.path.exists(_POSE_MODEL_PATH):
        # realpath: on the Orange Pi 4 Pro image the path is a link into
        # ~/ember-deployment/shared, so the download serves every later release. The .part
        # file: a download cut short (no network, power) never looks like a model.
        target = os.path.realpath(_POSE_MODEL_PATH)
        print(f"[FALL] Downloading pose landmarker model → {target} (~12 MB) ...")
        urllib.request.urlretrieve(_POSE_MODEL_URL, target + ".part")
        os.replace(target + ".part", target)
        print("[FALL] Model downloaded.")

def _open_pose(force_cpu=False):
    """Pose in its own process (features/vision_process.py): in Ember's process it held the
    GIL and starved the mic and Gemini I/O. The RK3588 NPU (checked full model) when ready
    and camera.pose_model is lite/full, else MediaPipe with camera.pose_model; heavy and
    force_cpu always use MediaPipe on the CPU. None if the process could not start (the
    camera thread then tries once on the CPU, or keeps video only)."""
    from functools import partial
    from features.vision_process import Remote
    from features.pose.backend import ChildPose
    try:
        return Remote(partial(ChildPose, _POSE_MODEL_PATH,
                              allow_npu=not force_cpu and _POSE_MODEL != "heavy"))
    except Exception as error:
        print(f"[CAM] Pose process could not start{' on the CPU' if force_cpu else ''} ({error}).", flush=True)
        return None

# Configuration
# The USB speaker (Jieli Technology "UACDemoV1.0") is not exposed as its own
# PortAudio device — PortAudio only sees the 'pipewire'/'default' nodes, which
# follow whatever PipeWire's default sink happens to be. So we pin the default
# sink to the USB speaker first, then open the pipewire node.
SPEAKER_SINK_MATCH = tuple(device_config.get("audio.speaker_match"))  # default ("UACDemo", "Jieli")

def pin_pipewire_sink(match=SPEAKER_SINK_MATCH):
    """Point PipeWire's default sink at the USB speaker. Returns its name, or None."""
    try:
        # errors="replace": pactl prints junk bytes in the state column for
        # the Jieli speaker, which would otherwise fail the whole listing.
        listing = subprocess.run(
            ["pactl", "list", "sinks", "short"],
            capture_output=True, text=True, errors="replace", timeout=5,
        ).stdout
    except Exception as e:
        print(f"[AUDIO] Could not query PipeWire sinks: {e}")
        return None

    for line in listing.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        sink_name = fields[1]
        if not any(m.lower() in sink_name.lower() for m in match):
            continue
        try:
            subprocess.run(
                ["pactl", "set-default-sink", sink_name],
                check=True, capture_output=True, timeout=5,
            )
            print(f"[AUDIO] Default sink pinned to USB speaker: {sink_name}")
            return sink_name
        except Exception as e:
            print(f"[AUDIO] Failed to pin default sink to {sink_name}: {e}")
            return None

    print("[AUDIO] USB speaker sink not found; leaving PipeWire default sink as-is.")
    return None

def get_default_device_id():
    if os.getenv("BAYMAX_HARDWARE_PROFILE") == "pc":
        from pc_hardware import select_audio_devices
        return select_audio_devices(sd)
    pin_pipewire_sink()

    devices = sd.query_devices()
    microphone = None

    for i, dev in enumerate(devices):
        if dev['name'] == 'pipewire':
            microphone = i
            print(f"FOUND PIPEWIRE AT {microphone}")
            break

    def find_output(predicate):
        for i, dev in enumerate(devices):
            if dev['max_output_channels'] > 0 and predicate(dev):
                return i
        return None

    # Prefer the USB speaker's own PortAudio device if ALSA ever exposes it,
    # otherwise route through pipewire (and fall back to 'default').
    speaker = find_output(
        lambda d: any(m.lower() in d['name'].lower() for m in SPEAKER_SINK_MATCH)
    )
    if speaker is not None:
        print(f"[AUDIO] USB speaker exposed directly at device {speaker}: {devices[speaker]['name']}")
    else:
        speaker = find_output(lambda d: d['name'] == 'pipewire')
        if speaker is None:
            speaker = find_output(lambda d: d['name'] == 'default')

    if speaker is None:
        speaker = 0
    return microphone, speaker

target_device_microphone, target_device_speaker = get_default_device_id()
print(f"[AUDIO] Mapping input to device ID: {target_device_microphone}, and output to device ID: {target_device_speaker} ({sd.query_devices()[target_device_speaker]['name']})")
sd.default.device = [target_device_microphone, target_device_speaker]

# ________________________________________________________________________________________________________________________________________________________________

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
# gemini-2.5-flash now returns 404 ("no longer available to new users").
SUMMARY_MODEL = "gemini-3.5-flash"  # lighter model for memory summaries

# ─── Memory retrieval config ─────────────────────────────────────────────────
# memory.retrieval_enabled = false (device.toml) skips loading the memory
# store at startup, which turns off every use of stored memories: the copy
# in the system prompt, the per-turn lookup and the initiation hints. Session
# summaries are still saved.
MEMORY_RETRIEVAL_ENABLED = device_config.get("memory.retrieval_enabled")
MEMORY_TOP_K = 3                 # how many memories to retrieve per turn
MEMORY_MAX_DISTANCE = 1.0        # cosine distance threshold (0=identical, 2=opposite)
MEMORY_WORD_WINDOW = 30          # use last N words of user speech for retrieval query
MEMORY_PROMPT_COUNT = 50         # newest memories copied into the system instruction

# ─── Memory saving during uptime ─────────────────────────────────────────────
# The conversation since the last save is summarised (SUMMARY_MODEL) and stored
# after this many user turns, this much text, or this long with nobody talking,
# so a crash or power cut loses at most that much. The stop path saves the rest.
MEMORY_SAVE_USER_TURNS = 20
MEMORY_SAVE_CHARS = 8000         # e.g. people talking among themselves for a while
MEMORY_SAVE_IDLE_SECONDS = 180.0
MEMORY_SAVE_RETRY_SECONDS = 300.0  # after a failed summary or store
MEMORY_SUMMARY_TIMEOUT = 20.0    # seconds for one summary call during uptime
# The last save must be done this many seconds after a stop begins. The MVP
# launcher sets 10: its service has 15 s (TimeoutStopSec) before systemd kills
# it, and the eyes take up to 3 s to close after Ember. Other stops allow more
# (Docker 120 s, the robot image's service 150 s, a terminal no limit).
MEMORY_STOP_SECONDS = float(os.getenv("BAYMAX_MEMORY_STOP_SECONDS", "60"))
MEMORY_CONTEXT_UTTERANCES = 4    # saved utterances kept as context for the next summary
MEMORY_CONTEXT_CHARS = 600       # each cut to its last this many characters
MEMORY_MAX_UNSAVED_CHARS = 60000  # if summaries keep failing, the oldest text goes

# ─── Reconnection ────────────────────────────────────────────────────────────
RECEIVE_FAILURES_BEFORE_RECONNECT = 3  # in a row with no message between them
# A healthy send takes milliseconds. If the network goes silent, with no close and no
# reset, a send waits until the kernel gives up (about 15 minutes on Linux), and the
# websocket's keepalive gets stuck behind it.
MIC_SEND_TIMEOUT_S = 5

# ─── Interruption gate ───────────────────────────────────────────────────────
INTERRUPT_RMS_THRESHOLD = 1000

# ─── Static / crackle noise suppression (mic → Gemini path) ──────────────────
# Streaming spectral-subtraction denoiser for the Gemini-bound mic stream (the
# WAV recorder keeps RAW audio so voice-biomarker jitter/shimmer is untouched).
#
# NOTE: default OFF. Measured on this unit's recordings, the "crackle" is HARD
# CLIPPING (~2.4% of samples pinned at full scale, RMS ~-7 dBFS) caused by the
# mic gain being far too hot — NOT additive hiss. Clipping is baked into the
# samples at the ADC and cannot be removed in software; enabling suppression on
# a clipped signal makes the noise estimate track the clipping and guts speech
# along with it. Fix the gain first (drop mic gain ~12 dB so speech peaks stop
# hitting the rails). Once the input is clean and any residual is true additive
# hiss, flip this to True — the subtraction below is designed for that case.
# NOISE SUPPRESSION DISABLED (v10)
# NOISE_SUPPRESSION_ENABLED = False
# NS_FRAME = 1024            # STFT window (samples) — matches the mic block size
# NS_HOP = 512              # 50% overlap-add hop
# NS_OVERSUBTRACT = 1.8     # alpha — how hard to subtract the noise estimate
# NS_SPECTRAL_FLOOR = 0.06  # beta — residual floor to limit "musical noise"
# NS_NOISE_ADAPT = 0.05     # EMA rate for tracking the noise spectrum on quiet frames
# NS_HP_CUTOFF_HZ = 90.0    # first-order high-pass to kill mains hum / rumble
# NOISE_GATE_ENABLED = True # force near-silence between words (helps the VAD end turns)
# NS_GATE_ATTEN = 0.08      # residual gain applied when a frame is classified silence

# ─── Fall detection config ───────────────────────────────────────────────────
# camera.pose_tracking = false (device.toml) skips MediaPipe Pose to save CPU.
# Video to Gemini and the MJPEG stream keep working, but fall detection and
# face presence need pose, so falls go undetected and the robot never starts
# a conversation on its own.
POSE_TRACKING_ENABLED = device_config.get("camera.pose_tracking")
FALL_DETECTION_ENABLED = device_config.get("fall_detection.enabled") and POSE_TRACKING_ENABLED
CAMERA_INDICES = device_config.get("camera.indices")
FALL_DETECTION_FPS = 10          # pose inference rate; 15 used ~50% more CPU for no detection gain
FALL_HISTORY_FRAMES = 6         # ~0.5 s of velocity history at 10 fps (was 8 at 15 fps)
FALL_ANGLE_THRESHOLD = 60.0     # v8/v9 used 45; at 45 a seated, leaning person kept triggering
FALL_DBG_INTERVAL_S = 1.0       # at most one [FALL DBG] line per second
FALL_ANG_VEL_THRESHOLD = 25.0
FALL_HIP_VEL_THRESHOLD = 0.12
FALL_CONFIRMATION_FRAMES = 2
FALL_COOLDOWN_SECONDS = 3.0
FALL_STAY_DOWN_SECONDS = 2.0     # must stay down this long; a bend-over comes back up

# ─── Conversation initiation config ──────────────────────────────────────────
# The old silence/face-presence coin flip is retained only as an explicit demo
# option. Normal conversation initiation comes from actual audio/video context.
RANDOM_INITIATION_ENABLED = os.getenv("BAYMAX_RANDOM_INITIATION", "0") == "1"
# The robot enters "initiation mode" after it has been silent for this long.
# TODO: change to 900 (15 minutes) for production — 60s is only for testing.
IDLE_THRESHOLD_SECONDS = 60.0
# Also reset the idle timer when the USER speaks (safety net so we never enter
# initiation mode while the user is actively talking, even if the robot happens
# to stay quiet). Set False to track robot speech only.
RESET_IDLE_ON_USER_SPEECH = True

# Face-presence clock: how long the face may vanish before the clock resets to 0.
# Brief look-aways (<= this) keep the clock running; longer absence restarts it.
FACE_ABSENCE_RESET_SECONDS = 10.0
# A face is "currently visible" only if it was seen within this many seconds.
FACE_FRESH_SECONDS = 1.0
# Min landmark visibility for nose + both eyes to count the face as detected.
FACE_VISIBILITY_THRESHOLD = 0.5

# Coin-flip cadence (Δ) and the horizon we precompute the coin table out to.
INITIATION_TICK_SECONDS = 5.0
INITIATION_HORIZON_SECONDS = 600.0
# Don't open a conversation until the user has been silent this long (gating).
INITIATION_USER_SILENCE_GATE = 1.5

# ── Authored CDF F(t): sigmoid ──
# F(t) = probability the robot has initiated BY t seconds of continuous face
# presence. Author the shape via the midpoint (t0) and steepness (k). Defaults
# give ≈48% by 30s and ≈95% by 60s of face presence (tuned for the 60s test
# config above — rescale t0 upward when IDLE_THRESHOLD_SECONDS goes to 15 min).
F_MIDPOINT = 30.0     # t0 — seconds of face presence at the curve's centre
F_STEEPNESS = 0.10    # k  — how sharply the curve ramps


def _initiation_cdf(t: float) -> float:
    """Authored sigmoid CDF, normalised so F(0)=0 and F(∞)=1."""
    if t <= 0.0:
        return 0.0
    raw = 1.0 / (1.0 + math.exp(-F_STEEPNESS * (t - F_MIDPOINT)))
    raw0 = 1.0 / (1.0 + math.exp(F_STEEPNESS * F_MIDPOINT))  # raw at t=0
    return (raw - raw0) / (1.0 - raw0)


def _build_hazard_table() -> list:
    """Precompute the per-tick coin h[k] for interval [k·Δ, (k+1)·Δ].

    h = (F(b) - F(a)) / (1 - F(a)) — the window's new probability mass divided
    by the fraction still silent at the start of the window (survival). Rolling
    h at each tick reproduces F(t) exactly across the tick grid.
    """
    n = int(INITIATION_HORIZON_SECONDS / INITIATION_TICK_SECONDS)
    table = []
    for k in range(n):
        a = k * INITIATION_TICK_SECONDS
        b = (k + 1) * INITIATION_TICK_SECONDS
        fa, fb = _initiation_cdf(a), _initiation_cdf(b)
        h = (fb - fa) / (1.0 - fa) if fa < 1.0 else 1.0
        table.append(max(0.0, min(1.0, h)))
    return table


_HAZARD_TABLE = _build_hazard_table()

# Interruption RMS floor reused as "user is speaking" signal (see gating below).

# MVP (#15): Ember speaks with the MVP persona (features/persona.py), as it
# did on v8. The toolkit and eye instructions are appended at session start
# (gemini_tools.extend_config); stored memories are prepended in _build_config.
_BASE_SYSTEM_INSTRUCTION = persona.PERSONA
if MEMORY_RETRIEVAL_ENABLED:
    _BASE_SYSTEM_INSTRUCTION += (
        "\n[MEMORY CONTEXT]\nYou may occasionally receive a '[MEMORY CONTEXT]' "
        "message with facts recalled from earlier conversations. Use them "
        "naturally alongside what you already know.\n"
    )

# Answer only speech meant for Ember, not people talking among themselves.
# Proactive audio lets Gemini stay silent on a turn; the instruction says when.
ONLY_WHEN_ADDRESSED = device_config.get("conversation.only_when_addressed")
if ONLY_WHEN_ADDRESSED:
    _BASE_SYSTEM_INSTRUCTION += (
        "\n[WHEN TO SPEAK]\nPeople in the room often talk to each other, not to "
        "you. Respond only when someone is talking to you: they say your name "
        "(Ember), they ask you something directly, or they are clearly "
        "continuing a conversation with you. Even then, do not answer an "
        "unfinished phrase, hesitation or listening acknowledgment merely because "
        "a pause occurred. Stay silent until there is a complete thought or a "
        "clear need for a response; judge meaning rather than utterance length. "
        "Someone facing or looking toward "
        "you while speaking is supporting evidence that they mean you, but body "
        "language alone is not certain. Your name is not required for a direct "
        "question. Ordinary pauses do not end an established conversation; "
        "do not require repeated invitations. Use the current conversation and "
        "clear visual cues as context, not proof of who is speaking. If the speech is people talking "
        "among themselves, stay silent and do not respond at all. Messages that "
        "start with a label in square brackets, like [FALL ALERT] or [REMINDER], "
        "come from your own systems and always need a response; never say the "
        "label itself out loud.\n"
    )

# Optional strict transcript gate. Off by default: its name/20 s rule drops
# direct questions after an ordinary pause. Gemini still judges conversational intent.
from features.conversational_ability.addressing import AddressGate
from features.conversational_ability.session import connection_lost
_address_gate = AddressGate(enabled=bool(ONLY_WHEN_ADDRESSED) and os.getenv("BAYMAX_ADDRESS_GATE", "0") == "1")

_BASE_CONFIG = {
    "response_modalities": ["AUDIO"],
    "input_audio_transcription": {},
    "output_audio_transcription": {},
    "speech_config": {
        "voice_config": {"prebuilt_voice_config": {"voice_name": "Fenrir"}}
    },
    "thinking_config": {
        "thinking_budget": 0
    },
    **({"proactivity": {"proactive_audio": True}} if ONLY_WHEN_ADDRESSED else {}),
    "realtime_input_config": {
        "automatic_activity_detection": {
            "disabled": False,
            "start_of_speech_sensitivity": types.StartSensitivity.START_SENSITIVITY_LOW,
            "end_of_speech_sensitivity": types.EndSensitivity.END_SENSITIVITY_LOW,
            "prefix_padding_ms": 20,
            "silence_duration_ms": 100,
        }
    }
}

from features.conversational_ability.processing import extend_config as audio_config
_BASE_CONFIG = audio_config(_BASE_CONFIG)


def _build_config() -> dict:
    """Build session config, injecting all stored memories into the system instruction."""
    system_instruction = _BASE_SYSTEM_INSTRUCTION

    if _memory_embedder is not None:
        try:
            # The newest stored memories — facts already summarised and vetted by
            # Gemini Flash from earlier conversations.
            docs = _newest_memories()
            if docs:
                mem_block = "\n".join(f"- {d}" for d in docs)
                system_instruction = (
                    "What you know about this user from previous conversations "
                    "(treat these as established facts — do NOT second-guess or "
                    "contradict them):\n"
                    + mem_block
                    + "\n\n"
                    + system_instruction
                )
                print(f"[MEMORY] Injected {len(docs)} memories into system instruction")
        except Exception as e:
            print(f"[MEMORY] Could not load memories for system instruction: {e}")

    return public_toolkit.extend_config({**_BASE_CONFIG, "system_instruction": system_instruction})

# Audio Config
SEND_SAMPLE_RATE = 48000
RECEIVE_SAMPLE_RATE = 48000
INPUT_CHANNELS = 2  # Hardware demands 2 channels
if os.getenv("BAYMAX_HARDWARE_PROFILE") == "pc":
    INPUT_CHANNELS = min(2, int(sd.query_devices(target_device_microphone)["max_input_channels"]))
from features.conversational_ability.processing import create as create_audio_enhancer
_audio_enhancer = create_audio_enhancer(SEND_SAMPLE_RATE)
from features.conversational_ability.waiting import create as create_waiting_cue
from features.conversational_ability.volume import voice_volume
_waiting_cue = create_waiting_cue(_audio_enhancer is not None and _audio_enhancer.full_duplex)
monitor.waiting_audio = _waiting_cue

OUTPUT_CHANNELS = 1 # Gemini returns mono
CHUNK_SIZE = 1024

# ─── Queues & Buffers ─────────────────────────────────────────────────────────
audio_queue_mic = asyncio.Queue()

# Thread-safe playback buffer for callback-based output stream
_playback_buffer = b""
_playback_lock = threading.Lock()

_gemini_speaking = False
_gemini_speaking_lock = threading.Lock()
_cut_off_prompts = []  # Ember's own messages for the next connection to send again

# ─── Transcript Accumulation ──────────────────────────────────────────────────
# The conversation in order, one [speaker, text] entry per utterance. Entries
# before _transcript_saved are stored as memories already and kept only as
# context for the next summary; each save trims the rest of them, so a robot
# that runs for days holds only what it hasn't saved yet.
_transcript = []
_transcript_saved = 0          # entries [0:saved) are stored as memories
_transcript_claimed = 0        # entries [0:claimed) are stored or being summarised
_transcript_unsaved_chars = 0  # text in entries [saved:)
_transcript_last_ts = 0.0      # time.monotonic() of the last fragment
_transcript_lock = threading.Lock()
# Post-session voice analysis needs every word the user said this run. The MVP
# launcher turns it off (BAYMAX_VOICE_ANALYSIS=0), and then nothing is kept here.
_VOICE_ANALYSIS_TRANSCRIPT = os.getenv("BAYMAX_VOICE_ANALYSIS", "1") != "0"
# Capped like the audio it goes with (DAY_UTTERANCE_RETENTION_DAYS) on a robot
# that runs for days: 50,000 fragments are a few days of talk.
_transcript_user = collections.deque(maxlen=50000)  # the user's fragments, for voice analysis only


def _transcript_add(speaker, text):
    """Record one transcription fragment. Consecutive fragments of a speaker form
    one utterance, joined as received ("A" + "mber"), but never extend one that
    is saved or being summarised."""
    global _transcript_unsaved_chars, _transcript_last_ts
    if not text or not text.strip():
        return
    with _transcript_lock:
        if len(_transcript) > _transcript_claimed and _transcript[-1][0] == speaker:
            _transcript[-1][1] += text
        else:
            _transcript.append([speaker, text])
        _transcript_unsaved_chars += len(text)
        _transcript_last_ts = time.monotonic()
        if speaker == "user" and _VOICE_ANALYSIS_TRANSCRIPT:
            _transcript_user.append(text.strip())
        if _transcript_unsaved_chars > MEMORY_MAX_UNSAVED_CHARS and _transcript_claimed == _transcript_saved:
            # Summaries keep failing (or memory isn't installed): drop the oldest
            # unsaved text rather than grow without limit.
            dropped = 0
            while _transcript_unsaved_chars > MEMORY_MAX_UNSAVED_CHARS * 3 // 4:
                oldest = _transcript[_transcript_saved]
                cut = min(len(oldest[1]), _transcript_unsaved_chars - MEMORY_MAX_UNSAVED_CHARS * 3 // 4)
                if cut == len(oldest[1]) and len(_transcript) > _transcript_saved + 1:
                    _transcript.pop(_transcript_saved)
                else:
                    oldest[1] = oldest[1][cut:]
                _transcript_unsaved_chars -= cut
                dropped += cut
            print(f"[MEMORY] Dropped the oldest {dropped} characters of unsaved conversation: "
                  f"over {MEMORY_MAX_UNSAVED_CHARS} were waiting to be saved")

# Rolling buffer of recent user words for memory retrieval queries
_recent_user_words = []
_recent_user_words_lock = threading.Lock()

# ─── Shared camera frame (fall detection thread → Gemini video sender) ──────
_latest_frame = None
_latest_frame_lock = threading.Lock()

# ─── Fall detection event (thread → async monitor) ──────────────────────────
_fall_detected_event = threading.Event()
_fall_held_note = ""  # a fall alert to send again keeps its first try's note that playback paused
_shutdown_event = threading.Event()
_stop_started_ts = 0.0  # time.monotonic() when the session was cancelled to stop

# ─── Conversation-initiation shared state ────────────────────────────────────
# Face visibility is computed in the fall-detection thread (it already has the
# pose landmarks each frame) and read by the async initiation monitor.
_face_last_seen_ts = 0.0          # time.monotonic() of the last frame a face was visible
_face_state_lock = threading.Lock()

# Timestamps used by the idle timer and the fire gating (all time.monotonic()).
_last_robot_speech_ts = time.monotonic()  # last time the robot produced audio
_last_user_speech_ts = 0.0                # last time the user was heard (transcription)
_last_loud_mic_ts = 0.0                   # last mic frame above the speech RMS floor
_initiation_ts_lock = threading.Lock()

# ─── Audio recording for voice biomarker analysis ───────────────────────────
DAY_UTTERANCE_DIR = os.path.join(DATA_DIR, "day_utterance")
os.makedirs(DAY_UTTERANCE_DIR, exist_ok=True)
_audio_record_queue = queue.Queue(maxsize=5000)

# Segments are meant to be transient: VoiceAnalyzer.concatenate_wavs() merges
# them at the end of a session and deletes them. That only happens on a clean
# shutdown, so a power cut or a hard kill leaves them behind forever — which is
# how this directory reached 18 GB / 3,329 files across six weeks.
#
# A full disk is not a cosmetic problem here. baymax_cloud.py queues telemetry
# to disk, so once the filesystem fills, fall events stop being recorded at all.
DAY_UTTERANCE_RETENTION_DAYS = float(os.getenv("BAYMAX_AUDIO_RETENTION_DAYS", "1"))
_PRUNE_EVERY_N_SEGMENTS = 60  # segments are 60s, so roughly hourly


def _prune_day_utterance():
    """Delete recorded segments older than the retention window."""
    if DAY_UTTERANCE_RETENTION_DAYS <= 0:
        return  # retention disabled

    cutoff = time.time() - DAY_UTTERANCE_RETENTION_DAYS * 86400
    removed = freed = 0

    for path in glob.glob(os.path.join(DAY_UTTERANCE_DIR, "seg_*.wav")):
        try:
            st = os.stat(path)
            if st.st_mtime >= cutoff:
                continue
            os.remove(path)
            removed += 1
            freed += st.st_size
        except OSError:
            # Being written, already gone, or permission denied — skip it.
            continue

    if removed:
        print(f"[RECORD] Pruned {removed} segments older than "
              f"{DAY_UTTERANCE_RETENTION_DAYS:g} days ({freed / 1e9:.2f} GB freed)")

# ─── Memory Retrieval Embedder (loaded once at startup) ──────────────────────
_memory_embedder: SemanticEmbedder = None  # set in __main__

def _load_memory_embedder():
    """Load SemanticEmbedder for memory retrieval.  Returns None on failure."""
    try:
        embedder = SemanticEmbedder(
            model_dir=ONNX_MODEL_DIR,
            chroma_dir=CHROMA_DIR,
            collection_name="user_memories",
            verbose=True,
        )
        count = embedder._collection.count() if embedder._collection else 0
        print(f"[MEMORY] Embedder ready — {count} memories in store")
        return embedder
    except Exception as e:
        print(f"[MEMORY] WARNING — could not load embedder: {e}")
        print("[MEMORY] Memory retrieval will be disabled this session.")
        return None


def _memory_model_to_download():
    """Memory's packages are installed but its model isn't there yet: boards without
    torch (the Orange Pi) can't run convert_model.py, so Ember fetches the model."""
    return not missing_packages() and not model_present(ONNX_MODEL_DIR)


def _download_memory_model():
    """First start only: fetch the pinned, checksummed model (about 90 MB) in the
    background, then turn memory on. Ember starts and talks meanwhile; a first start
    has no memories to recall yet."""
    global _memory_embedder
    print("[MEMORY] Downloading the memory model (about 90 MB, once) in the background...")
    try:
        download_model(ONNX_MODEL_DIR)
    except Exception as e:
        print(f"[MEMORY] WARNING — could not download the model: {e}")
        print("[MEMORY] Memory retrieval will be disabled this session; the download is retried next start.")
        return
    _memory_embedder = _load_memory_embedder()


def _retrieve_memories(query_text: str) -> str:
    if _memory_embedder is None:
        return ""
    if not query_text.strip():
        return ""

    try:
        results = _memory_embedder.search(
            query_text,
            n_results=MEMORY_TOP_K,
        )
    except Exception as e:
        print(f"[MEMORY] Search error: {e}")
        return ""

    if not results:
        return ""

    # Filter by distance threshold
    relevant = [r for r in results if r["distance"] <= MEMORY_MAX_DISTANCE]
    if not relevant:
        return ""

    # Build context string
    memory_lines = []
    for r in relevant:
        memory_lines.append(f"- {r['document']} (relevance: {1 - r['distance']:.2f})")

    context = (
        "[MEMORY CONTEXT] Here are things you remember about this user "
        "from previous conversations:\n"
        + "\n".join(memory_lines)
    )
    print(f"[MEMORY] Retrieved {len(relevant)} memories for context")
    for line in memory_lines:
        print(f"  {line}")
    return context


def _newest_memories(limit=MEMORY_PROMPT_COUNT):
    """The `limit` most recently saved memories, oldest first, so a newer fact
    comes after the one it updates. Chroma's get() lists memories in the order
    they were added, so get(limit=50) gave the first 50 ever saved; this sorts
    on their "timestamp" (YYYYMMDD_HHMMSS) instead. The sort is stable, so lines
    saved in the same second keep the order they were added in."""
    collection = _memory_embedder._collection if _memory_embedder is not None else None
    if collection is None or collection.count() == 0:
        return []
    got = collection.get(include=["metadatas"])
    order = sorted(range(len(got["ids"])), key=lambda i: str((got["metadatas"][i] or {}).get("timestamp", "")))
    ids = [got["ids"][i] for i in order[-limit:]]
    docs = collection.get(ids=ids, include=["documents"])
    by_id = dict(zip(docs["ids"], docs["documents"]))
    return [by_id[i] for i in ids if by_id.get(i)]


# ─── Latency Profiling ────────────────────────────────────────────────────────
PROFILE_WINDOW = 50

class LatencyTracker:
    def __init__(self, name: str, window: int = PROFILE_WINDOW):
        self.name = name
        self.samples = collections.deque(maxlen=window)
        self._count = 0
        self._report_every = window

    def record(self, duration_ms: float):
        self.samples.append(duration_ms)
        self._count += 1
        if self._count % self._report_every == 0:
            self._print_summary()

    def _print_summary(self):
        arr = np.array(self.samples)
        with _playback_lock:
            pbuf = len(_playback_buffer)
        print(
            f"[PROFILE] {self.name:.<30s} "
            f"n={len(arr):>4d}  "
            f"avg={arr.mean():7.1f} ms  "
            f"p50={np.percentile(arr, 50):7.1f} ms  "
            f"p95={np.percentile(arr, 95):7.1f} ms  "
            f"max={arr.max():7.1f} ms  "
            f"queue_mic={audio_queue_mic.qsize():>4d}  "
            f"pbuf={pbuf:>6d}"
        )

tracker_mic_queue = LatencyTracker("mic_queue_wait")
tracker_send_audio = LatencyTracker("send_audio_to_gemini")
tracker_send_video = LatencyTracker("send_video_to_gemini")
tracker_receive = LatencyTracker("receive_from_gemini")
tracker_roundtrip = LatencyTracker("roundtrip_estimate")
tracker_first_audio = LatencyTracker("first_audio_latency")
_last_mic_send_ts: float = 0.0

# ─── Playback buffer helpers ─────────────────────────────────────────────────
def _append_playback(data: bytes, *, new_turn=False, timing_id=None):
    global _playback_buffer
    with _playback_lock:
        if new_turn and _playback_buffer:
            # Separate queued replies without delaying a reply after silence.
            _playback_buffer += b"\x00" * (RECEIVE_SAMPLE_RATE * OUTPUT_CHANNELS * 2 * 180 // 1000)
        if timing_id is not None:
            nonzero = np.flatnonzero(np.frombuffer(data, dtype=np.int16))
            if len(nonzero):
                monitor.timing.queued(timing_id, len(_playback_buffer), int(nonzero[0]))
        _playback_buffer += data

def _flush_playback():
    global _playback_buffer
    with _playback_lock:
        _playback_buffer = b""
        monitor.timing.clear_playback()

def _abandon_reply():
    """A lost connection ends the reply it was streaming. Without this the rest of
    the queued audio plays after the reconnect, with the mic muted, out of nowhere.
    Ember's own messages (a fall check-in, a family message) are sent again instead."""
    global _gemini_speaking
    with _playback_lock:
        audio_cut_off = bool(_playback_buffer)
    _flush_playback()
    monitor.timing.complete_reply(interrupted=True)
    with _gemini_speaking_lock:
        audio_cut_off = audio_cut_off or _gemini_speaking
        _gemini_speaking = False
    _cut_off_prompts.extend(_address_gate.abandon(audio_cut_off))

async def _send_cut_off_prompts(session):
    """Send what the last connection cut off, one at a time. A message whose send fails
    (this connection drops too, or the session ends meanwhile) goes back to the front
    for the next connection: it never reached the gate, so `abandon` can't return it,
    and its sender, whose own send succeeded, won't retry it. A sent one is out of the
    list, and the gate notes it with no pause before the send returns, so nothing is
    sent twice."""
    while _cut_off_prompts:
        args, kwargs = _cut_off_prompts.pop(0)
        print("[SESSION] Sending Ember's cut-off message again.", flush=True)
        try:
            await session.send_client_content(*args, **kwargs)
        except BaseException:
            _cut_off_prompts.insert(0, (args, kwargs))
            raise

def _abort_connection(session):
    """Leaving connect() closes the socket politely, which on a network that has gone
    silent also waits until the kernel gives up (about 15 minutes on Linux). Drop it first."""
    try:
        session._ws.transport.abort()
    except Exception as e:
        print(f"[SESSION] Could not abort the connection: {e}", flush=True)

# ─── Callback-based output stream ────────────────────────────────────────────
def _output_callback(outdata, frames, time_info, status):
    global _playback_buffer
    monitor.timing.output_status(status)
    n_bytes = frames * 2
    with _playback_lock:
        chunk = _playback_buffer[:n_bytes]
        _playback_buffer = _playback_buffer[n_bytes:]
        current, dac = getattr(time_info, 'currentTime', 0), getattr(time_info, 'outputBufferDacTime', 0)
        delay = max(0, dac - current) if current > 0 and dac > 0 else getattr(_audio_enhancer, 'output_latency_ms', 0) / 1000
        monitor.timing.played(len(chunk), delay, RECEIVE_SAMPLE_RATE)
    if _waiting_cue is not None:
        if _audio_enhancer is not None and _audio_enhancer.full_duplex:
            chunk = _waiting_cue.render(chunk, n_bytes)
        else:
            _waiting_cue.cancel()
    if len(chunk) < n_bytes:
        chunk += b"\x00" * (n_bytes - len(chunk))
    chunk = voice_volume.apply(chunk)  # Ember's voice only, before the echo reference
    if _audio_enhancer is not None:
        chunk = _audio_enhancer.mix_output(chunk)  # e.g. an audiobook playing
    outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, OUTPUT_CHANNELS)
    if _audio_enhancer is not None:
        _audio_enhancer.rendered(chunk, time_info)

def start_output_stream() -> sd.OutputStream:
    stream = sd.OutputStream(
        samplerate=RECEIVE_SAMPLE_RATE,
        channels=OUTPUT_CHANNELS,
        dtype="int16",
        blocksize=1024,
        callback=_output_callback,
    )
    if _audio_enhancer is not None:
        _audio_enhancer.output_latency_ms = float(stream.latency) * 1000
    stream.start()
    return stream

# ─── Queue monitor ────────────────────────────────────────────────────────────
async def monitor_queues(interval: float = 3.0):
    while True:
        await asyncio.sleep(interval)
        with _playback_lock:
            pbuf_len = len(_playback_buffer)
        with _transcript_lock:
            unsaved = _transcript[_transcript_saved:]
            user_count = sum(1 for speaker, _ in unsaved if speaker == "user")
            gemini_count = len(unsaved) - user_count
        print(
            f"[QUEUES] mic_queue={audio_queue_mic.qsize():>4d}  "
            f"playback_buf={pbuf_len:>6d} bytes  "
            f"unsaved utterances: user={user_count} gemini={gemini_count}"
        )

# ─── Audio Writer Thread (records user speech to WAV) ────────────────────────
def _audio_writer_thread():
    """Drains _audio_record_queue and writes 60-second WAV segments to day_utterance/."""
    segment_duration = 60
    samples_per_segment = SEND_SAMPLE_RATE * segment_duration
    segment_index = 0
    current_samples = 0
    wf = None

    def _open_new_segment():
        nonlocal wf, current_samples, segment_index
        if wf:
            wf.close()

        # Prune before opening, so a long-running session cannot accumulate
        # weeks of audio between restarts.
        if segment_index % _PRUNE_EVERY_N_SEGMENTS == 0:
            _prune_day_utterance()

        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(DAY_UTTERANCE_DIR, f"seg_{ts}_{segment_index:04d}.wav")
        wf = wave.open(path, "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SEND_SAMPLE_RATE)
        current_samples = 0
        segment_index += 1

    _open_new_segment()  # segment_index is 0 here, so this prunes on startup

    try:
        while not _shutdown_event.is_set() or not _audio_record_queue.empty():
            try:
                chunk = _audio_record_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if chunk is None:
                break
            wf.writeframes(chunk)
            current_samples += len(chunk) // 2
            if current_samples >= samples_per_segment:
                _open_new_segment()
    finally:
        if wf:
            wf.close()
    print(f"[RECORD] Audio writer stopped. {segment_index} segments written.")

# ─── Fall Detection Visualisation ────────────────────────────────────────────
_POSE_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 7),
    (0, 4), (4, 5), (5, 6), (6, 8),
    (11, 12), (11, 13), (13, 15),
    (12, 14), (14, 16),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (27, 31), (29, 31),
    (24, 26), (26, 28), (28, 30), (28, 32), (30, 32),
]
_WHITE  = (255, 255, 255)
_GREEN  = (0, 220, 0)
_YELLOW = (0, 200, 255)
_RED    = (30,  30, 220)
_DARK   = (20,  20,  20)
_CYAN   = (255, 220, 0)

def _text(img, text, pos, scale=0.65, color=_WHITE, thickness=2):
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX,
                scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX,
                scale, color, thickness, cv2.LINE_AA)

def _draw_skeleton(frame, landmarks):
    if not landmarks:
        return
    h, w = frame.shape[:2]
    pts = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]
    for a, b in _POSE_CONNECTIONS:
        if a < len(pts) and b < len(pts):
            if landmarks[a].visibility > 0.4 and landmarks[b].visibility > 0.4:
                cv2.line(frame, pts[a], pts[b], _GREEN, 2, cv2.LINE_AA)
    for i, (x, y) in enumerate(pts):
        if landmarks[i].visibility > 0.4:
            cv2.circle(frame, (x, y), 3, _WHITE, -1, cv2.LINE_AA)

def _draw_trunk_line(frame, landmarks):
    if not landmarks:
        return
    h, w = frame.shape[:2]
    ls, rs = landmarks[11], landmarks[12]
    lh, rh = landmarks[23], landmarks[24]
    sm = (int((ls.x + rs.x) / 2 * w), int((ls.y + rs.y) / 2 * h))
    hm = (int((lh.x + rh.x) / 2 * w), int((lh.y + rh.y) / 2 * h))
    cv2.line(frame, hm, sm, _YELLOW, 3, cv2.LINE_AA)
    cv2.circle(frame, sm, 7, _YELLOW, -1, cv2.LINE_AA)
    cv2.circle(frame, hm, 7, _YELLOW, -1, cv2.LINE_AA)

def _draw_hud(frame, result, fps):
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (310, 115), _DARK, -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
    angle = result["trunk_angle"]
    ang_v = result["angular_vel"]
    hip_v = result["hip_descent_vel"]
    _text(frame, f"FPS:          {fps:5.1f}",                                         (10, 24),  color=_CYAN)
    _text(frame, f"Trunk angle:  {angle:.1f} deg" if angle is not None else "Trunk angle:  --", (10, 50))
    _text(frame, f"Angular vel:  {ang_v:.1f} deg/s" if ang_v is not None else "Angular vel:  --", (10, 76))
    _text(frame, f"Hip descent:  {hip_v:.2f} /s" if hip_v is not None else "Hip descent:  --", (10, 102))

def _draw_fall_alert(frame):
    h, w = frame.shape[:2]
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, h), _RED, -1)
    cv2.addWeighted(overlay, 0.35, frame, 0.65, 0, frame)
    label = "FALL DETECTED"
    font, scale, thick = cv2.FONT_HERSHEY_DUPLEX, 2.0, 3
    (tw, th), _ = cv2.getTextSize(label, font, scale, thick)
    x, y = (w - tw) // 2, (h + th) // 2
    cv2.putText(frame, label, (x + 4, y + 4), font, scale, (0, 0, 0), thick + 4, cv2.LINE_AA)
    cv2.putText(frame, label, (x, y),          font, scale, _WHITE,    thick,     cv2.LINE_AA)

def _draw_angle_graph(frame, angle_history, threshold):
    if len(angle_history) < 2:
        return
    h, w = frame.shape[:2]
    gw, gh = 200, 80
    gx, gy = w - gw - 10, h - gh - 10
    overlay = frame.copy()
    cv2.rectangle(overlay, (gx, gy), (gx + gw, gy + gh), _DARK, -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    ty = gy + gh - int(threshold / 90.0 * gh)
    cv2.line(frame, (gx, ty), (gx + gw, ty), _RED, 1)
    vals = list(angle_history)
    n = len(vals)
    for i in range(1, n):
        x0 = gx + int((i - 1) / (n - 1) * gw)
        x1 = gx + int(i       / (n - 1) * gw)
        y0 = gy + gh - int(min(vals[i - 1], 90) / 90.0 * gh)
        y1 = gy + gh - int(min(vals[i],     90) / 90.0 * gh)
        cv2.line(frame, (x0, y0), (x1, y1), _GREEN, 2, cv2.LINE_AA)
    _text(frame, "Angle (0-90)", (gx + 4, gy + 12), scale=0.40, color=_CYAN, thickness=1)

def _draw_annotated(frame, landmarks, result, fps_history, angle_history):
    """The dashboard's live view: overlays drawn on a copy of the camera frame."""
    fps = float(np.mean(fps_history))
    viz = frame.copy()
    _draw_skeleton(viz, landmarks)
    _draw_trunk_line(viz, landmarks)
    _text(viz, f"FPS: {fps:5.1f}", (10, 24), color=_CYAN)
    _draw_hud(viz, result, fps)
    _draw_angle_graph(viz, angle_history, FALL_ANGLE_THRESHOLD)

    if result["fall_active"]:
        _draw_fall_alert(viz)

    if not POSE_TRACKING_ENABLED:
        status, status_color = "Pose tracking: OFF", _CYAN
    elif not FALL_DETECTION_ENABLED:
        status, status_color = "Fall detection: OFF", _CYAN
    elif result["fall_active"]:
        status, status_color = "Status: FALL", _RED
    elif result["fall_pending"]:
        status, status_color = "Status: down? (checking)", _CYAN
    else:
        status, status_color = "Status: OK", _GREEN
    _text(viz, status, (10, viz.shape[0] - 12), color=status_color)
    return viz


# ─── Annotated frame for MJPEG stream ───────────────────────────────────────
_annotated_frame = None
_annotated_frame_lock = threading.Lock()
_mjpeg_viewers = 0              # /stream connections open (under _annotated_frame_lock)
_annotated_inputs = None        # what the latest frame's picture is drawn from, while not drawn

def _start_mjpeg_server(port=8080):
    """Lightweight MJPEG server in a daemon thread — serves annotated fall detection frames."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            global _mjpeg_viewers, _annotated_frame
            if self.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                with _annotated_frame_lock:
                    _mjpeg_viewers += 1          # the camera thread draws while someone watches
                sent = None
                try:
                    while not _shutdown_event.is_set():
                        with _annotated_frame_lock:
                            frame, inputs = _annotated_frame, _annotated_inputs
                        if frame is None and inputs is not None:
                            # The latest frame wasn't drawn (nobody was watching): draw
                            # it here; if the camera has stopped, it's the last one.
                            frame = _draw_annotated(*inputs)
                            with _annotated_frame_lock:
                                if _annotated_frame is None and _annotated_inputs is inputs:
                                    _annotated_frame = frame
                        if frame is None:
                            time.sleep(0.05)
                            continue
                        if frame is not sent:        # a frame already sent is re-sent, not re-encoded
                            _, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                            sent, data = frame, jpg.tobytes()
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                        self.wfile.write(data)
                        self.wfile.write(b"\r\n")
                        time.sleep(0.066)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with _annotated_frame_lock:
                        _mjpeg_viewers -= 1
            else:
                self.send_response(301)
                self.send_header("Location", "/stream")
                self.end_headers()

        def log_message(self, format, *args):
            pass

    # Loopback only: the dashboard serves this at /api/stream behind its login.
    server = HTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"[MJPEG] Video stream on http://127.0.0.1:{port}/stream (robot only; the dashboard proxies it at /api/stream)")
    server.serve_forever()

# ─── Face-presence detection (shared with initiation monitor) ────────────────
# MediaPipe Pose facial landmark indices.
_FACE_NOSE = 0
_FACE_LEFT_EYE = 2
_FACE_RIGHT_EYE = 5

def _update_face_visibility(landmarks):
    """Set _face_last_seen_ts if nose + both eyes are confidently visible.

    Uses the pose landmarks the fall thread already computes — no extra model.
    """
    if not landmarks:
        return
    try:
        nose = landmarks[_FACE_NOSE]
        leye = landmarks[_FACE_LEFT_EYE]
        reye = landmarks[_FACE_RIGHT_EYE]
    except (IndexError, TypeError):
        return
    if all(p.visibility >= FACE_VISIBILITY_THRESHOLD for p in (nose, leye, reye)):
        with _face_state_lock:
            global _face_last_seen_ts
            _face_last_seen_ts = time.monotonic()


# ─── Fall Detection Thread ──────────────────────────────────────────────────
def _fall_detection_thread():
    """Runs in a dedicated thread.  Captures camera frames, has pose run in its own
    process (_open_pose: the RK3588 NPU or MediaPipe on the CPU), draws visual
    overlays and shares frames with the Gemini video sender and the MJPEG stream,
    and runs fall detection.  The pose landmarks are also used for face presence
    (conversation initiation). If the pose process cannot start or later fails, it is
    started again once, on the CPU; video continues either way."""
    global _latest_frame, _annotated_frame, _annotated_inputs

    pose_model_ready = False
    if POSE_TRACKING_ENABLED:
        try:
            _ensure_pose_model()
            pose_model_ready = True
        except Exception as error:  # e.g. no network on a new release's first start: keep video
            print(f"[CAM] Could not download the pose model ({error}); video only, no fall detection "
                  "or face presence until Ember restarts.", flush=True)

    # fall_detection.enabled = false in device.toml turns off only the fall
    # check; pose tracking still feeds video and face presence.
    fall_detector = FallDetector(
        angle_threshold=FALL_ANGLE_THRESHOLD,
        ang_vel_threshold=FALL_ANG_VEL_THRESHOLD,
        hip_vel_threshold=FALL_HIP_VEL_THRESHOLD,
        history_window=FALL_HISTORY_FRAMES,
        confirmation_frames=FALL_CONFIRMATION_FRAMES,
        cooldown_seconds=FALL_COOLDOWN_SECONDS,
        stay_down_s=FALL_STAY_DOWN_SECONDS,
    ) if FALL_DETECTION_ENABLED else None
    no_fall = dict(fall_detected=False, fall_active=False, fall_pending=False,
                   trunk_angle=None, angular_vel=None, hip_descent_vel=None)

    cap = None
    from pc_hardware import camera_candidates, open_camera
    pc = os.getenv("BAYMAX_HARDWARE_PROFILE") == "pc"
    for cam_idx in (camera_candidates() if pc else CAMERA_INDICES):
        test = open_camera(cv2, cam_idx) if pc else cv2.VideoCapture(cam_idx)
        if test.isOpened():
            ret, _ = test.read()
            if ret:
                cap = test
                print(f"[FALL] Camera found at index {cam_idx}")
                break
        test.release()

    if cap is None:
        print("[CAM] Camera not available — video and face presence disabled.")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if pc:  # pc profile (run_pc.py: the Orange Pi, the LattePanda MVP); only the Orange Pi (arm_linux) keeps 1 frame
        from pc_hardware import latest_frames_only
        latest_frames_only(cv2, cap)
    # MVP features that watch the camera (greetings) get every frame. Its own CPU
    # fall detector stays off unless BAYMAX_FALL_DETECTION_ENABLED=1; v10's runs here.
    fall_worker.start(fps=FALL_DETECTION_FPS, shared_pose=POSE_TRACKING_ENABLED,  # one pose model, not two
                      angle_threshold=FALL_ANGLE_THRESHOLD,
                      ang_vel_threshold=FALL_ANG_VEL_THRESHOLD, hip_vel_threshold=FALL_HIP_VEL_THRESHOLD,
                      history_window=FALL_HISTORY_FRAMES, confirmation_frames=FALL_CONFIRMATION_FRAMES,
                      cooldown_seconds=FALL_COOLDOWN_SECONDS)
    if POSE_TRACKING_ENABLED:
        print(f"[CAM] Pose tracking active (640x480, {FALL_DETECTION_FPS} fps) — fall detection {'active' if FALL_DETECTION_ENABLED else 'OFF (device.toml)'}.")
    else:
        print("[CAM] Pose tracking OFF (camera.pose_tracking = false in device.toml): "
              "video only; no fall detection, no robot-initiated conversations.")

    frame_interval = 1.0 / FALL_DETECTION_FPS
    start_t = time.monotonic()
    prev_t = start_t
    angle_history = collections.deque(maxlen=60)
    fps_history = collections.deque(maxlen=30)
    last_dbg_t = 0.0

    landmarker = None
    pose_restarts = 0          # one restart on the CPU per run, at start or after a failure
    video_only = "[CAM] Pose unavailable; video only, no fall detection or face presence until Ember restarts."
    try:
        # Pose runs in its own process (_open_pose): in this one it held the GIL and
        # starved the mic callback and Gemini I/O (overflows, minute-late replies).
        if pose_model_ready:
            landmarker = _open_pose()
            if landmarker is None:
                # E.g. librknnrt crashed the child while it set up the NPU (before its first
                # reply): MediaPipe on the CPU never touches RKNN. This is the one restart.
                pose_restarts = 1
                print("[CAM] Starting the pose process again, on the CPU.", flush=True)
                landmarker = _open_pose(force_cpu=True)
                if landmarker is None:
                    print(video_only, flush=True)
        while not _shutdown_event.is_set():
            t0 = time.monotonic()

            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            # Share raw frame for Gemini video sender
            with _latest_frame_lock:
                _latest_frame = frame
            fall_worker.submit(frame)

            # Run pose detection
            landmarks = None
            if landmarker is not None:
                pose_t0 = time.monotonic()
                try:
                    landmarks = landmarker.detect(frame)
                except RuntimeError as error:  # vision process died/hung up or its model raised
                    landmarker.close()
                    landmarker = None
                    if not pose_restarts:
                        pose_restarts = 1
                        print(f"[CAM] Pose process failed ({error}); restarting it on the CPU.", flush=True)
                        landmarker = _open_pose(force_cpu=True)
                        if landmarker is None:
                            print(video_only, flush=True)
                    else:
                        print(f"[CAM] Pose process failed again ({error}); video only until Ember restarts.",
                              flush=True)
                else:
                    fall_worker.share(landmarks, round((time.monotonic() - pose_t0) * 1000, 1),
                                      device=landmarker.device)

            # Share face-presence signal with the initiation monitor
            _update_face_visibility(landmarks)

            result = fall_detector.update(landmarks) if fall_detector else no_fall
            if result["trunk_angle"] is not None:
                angle_history.append(result["trunk_angle"])
                a = result["trunk_angle"]
                av = result["angular_vel"]
                hv = result["hip_descent_vel"]
                # Velocities are None until the detector has two frames of
                # history; formatting None killed the whole camera thread.
                # Rate-limited: at 15 fps a person leaning past 30° would
                # otherwise log every frame.
                if (a > 30 and av is not None and hv is not None
                        and t0 - last_dbg_t >= FALL_DBG_INTERVAL_S):
                    last_dbg_t = t0
                    print(
                        f"[FALL DBG] angle={a:.1f}° "
                        f"ang_vel={av:.1f}°/s "
                        f"hip_vel={hv:.2f}/s "
                        f"streak={fall_detector._suspicious_streak}",
                        flush=True,
                    )

            # FPS
            now = time.monotonic()
            fps_history.append(1.0 / max(now - prev_t, 1e-6))
            prev_t = now

            # The annotated picture is only for the dashboard's live view
            # (7-20 ms of CPU a frame): draw it only while someone watches.
            with _annotated_frame_lock:
                watched = _mjpeg_viewers > 0
                if not watched:                  # a new viewer draws it from these
                    _annotated_frame = None
                    _annotated_inputs = (frame, landmarks, result,
                                         tuple(fps_history), tuple(angle_history))
            if watched:
                viz = _draw_annotated(frame, landmarks, result, fps_history, angle_history)
                # Share annotated frame for MJPEG stream
                with _annotated_frame_lock:
                    _annotated_frame = viz

            if result["fall_detected"]:
                print(
                    f"[FALL] *** FALL DETECTED *** "
                    f"angle={result['trunk_angle']:.1f}° "
                    f"ang_vel={result['angular_vel']:.1f}°/s "
                    f"hip_vel={result['hip_descent_vel']:.2f}/s",
                    flush=True,
                )
                _fall_detected_event.set()
                monitor.notify('fall_alert')  # surprised eyes
                _notify_webapp("/api/fall", {
                    "trunk_angle": result["trunk_angle"],
                    "angular_vel": result["angular_vel"],
                    "hip_descent_vel": result["hip_descent_vel"],
                })

            # Throttle to target FPS
            elapsed = time.monotonic() - t0
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    except Exception as e:
        print(f"[FALL] Fall detection thread error: {e}")
    finally:
        if landmarker is not None:
            landmarker.close()
        with _latest_frame_lock:
            _latest_frame = None  # Gemini gets no frozen picture once the camera stops.
        cap.release()
        fall_worker.stop()
        print("[FALL] Fall detection thread stopped.")


# ─── Static / crackle noise suppression ─────────────────────────────────────
# NOISE SUPPRESSION DISABLED (v10)
# class _SpectralDenoiser:
#     """Streaming spectral-subtraction denoiser for the mic → Gemini stream.
#
#     Removes stationary hiss and knocks down broadband crackle so Gemini's
#     server-side VAD sees clean silence between words. Runs inside the PortAudio
#     callback thread: numpy-only, a couple of 1024-pt FFTs per block, well under
#     a millisecond, with ~one-frame (~21 ms) algorithmic delay.
#
#     Works in int16 amplitude units (samples held as float32 in the
#     -32768..32767 range) so RMS values line up with INTERRUPT_RMS_THRESHOLD and
#     the rest of the pipeline.
#
#     Pipeline per block:
#       1. first-order high-pass  → kills mains hum / DC rumble
#       2. STFT spectral subtraction with a noise estimate adapted on quiet frames
#       3. optional expander/gate → forces near-silence between words for the VAD
#       4. 50%-overlap Hann OLA resynthesis (click-free at block boundaries)
#     """
#
#     def __init__(self, frame=NS_FRAME, hop=NS_HOP, sr=SEND_SAMPLE_RATE,
#                  alpha=NS_OVERSUBTRACT, beta=NS_SPECTRAL_FLOOR,
#                  adapt=NS_NOISE_ADAPT, hp_hz=NS_HP_CUTOFF_HZ,
#                  gate=NOISE_GATE_ENABLED, gate_atten=NS_GATE_ATTEN):
#         self.N = frame
#         self.H = hop
#         self.alpha = alpha
#         self.beta = beta
#         self.adapt = adapt
#         self.gate = gate
#         self.gate_atten = gate_atten
#         self.win = np.hanning(frame).astype(np.float32)
#
#         # First-order high-pass (y[n] = a*(y[n-1] + x[n] - x[n-1])) as a biquad
#         # run via scipy.lfilter with carried state — vectorised, no Python loop.
#         dt = 1.0 / sr
#         rc = 1.0 / (2.0 * math.pi * hp_hz)
#         a = rc / (rc + dt)
#         self._hp_b = np.array([a, -a], dtype=np.float32)
#         self._hp_a = np.array([1.0, -a], dtype=np.float32)
#         self._hp_zi = np.zeros(1, dtype=np.float32)  # filter state across blocks
#
#         # Streaming buffers
#         self._in = np.zeros(0, dtype=np.float32)
#         self._acc = np.zeros(frame, dtype=np.float32)   # OLA output accumulator
#         self._nrm = np.zeros(frame, dtype=np.float32)   # OLA window^2 accumulator
#         self._out = np.zeros(0, dtype=np.float32)
#
#         # Noise model + gate state
#         self.noise_mag = None       # magnitude spectrum of the noise floor
#         self.noise_level = 0.0      # EMA of quiet-frame RMS
#         self._warm = 0              # frames of initial noise learning
#         self._active = False        # gate hysteresis (speech vs silence)
#         self._gain = 1.0            # smoothed gate gain
#
#     def _highpass(self, x):
#         y, self._hp_zi = scipy_lfilter(self._hp_b, self._hp_a, x, zi=self._hp_zi)
#         return y.astype(np.float32)
#
#     def _update_noise(self, mag, fr_rms):
#         if self.noise_mag is None:
#             self.noise_mag = mag.copy()
#             self.noise_level = fr_rms
#             self._warm = 1
#             return
#         if self._warm < 10:
#             # First ~200 ms after (re)connect are assumed to be room noise.
#             self.noise_mag = 0.7 * self.noise_mag + 0.3 * mag
#             self.noise_level = 0.7 * self.noise_level + 0.3 * fr_rms
#             self._warm += 1
#             return
#         # Otherwise only fold quiet frames into the noise estimate so speech
#         # never leaks into (and then gets subtracted as) "noise".
#         if fr_rms < 2.5 * self.noise_level + 1.0:
#             self.noise_mag = (1 - self.adapt) * self.noise_mag + self.adapt * mag
#             self.noise_level = (1 - self.adapt) * self.noise_level + self.adapt * fr_rms
#
#     def process(self, mono_int16: np.ndarray) -> np.ndarray:
#         """Denoise one mic block; returns int16 samples of the same length."""
#         x = self._highpass(mono_int16.astype(np.float32))
#         self._in = np.concatenate([self._in, x])
#
#         while self._in.shape[0] >= self.N:
#             frame = self._in[:self.N] * self.win
#             spec = np.fft.rfft(frame)
#             mag = np.abs(spec)
#             phase = np.angle(spec)
#             fr_rms = float(np.sqrt(np.mean(frame * frame)))
#
#             self._update_noise(mag, fr_rms)
#
#             # Spectral subtraction with an over-subtraction factor and a floor
#             # (the floor trades a little residual hiss for far less musical noise).
#             clean_mag = np.maximum(mag - self.alpha * self.noise_mag, self.beta * mag)
#             rec = np.fft.irfft(clean_mag * np.exp(1j * phase),
#                                n=self.N).astype(np.float32)
#
#             # Expander/gate: drive near-silence when the frame reads as noise so
#             # the server-side VAD gets a clean end-of-turn. Hysteresis + gain
#             # smoothing avoid chopping quiet speech onsets and clicking.
#             if self.gate:
#                 open_th = 3.0 * self.noise_level + 1.0
#                 close_th = 1.8 * self.noise_level + 1.0
#                 self._active = (fr_rms > (close_th if self._active else open_th))
#                 target = 1.0 if self._active else self.gate_atten
#                 self._gain += 0.3 * (target - self._gain)
#                 rec *= self._gain
#
#             # 50%-overlap Hann synthesis + normalise by accumulated window^2.
#             rec *= self.win
#             self._acc += rec
#             self._nrm += self.win * self.win
#             done = self._acc[:self.H] / np.maximum(self._nrm[:self.H], 1e-6)
#             self._out = np.concatenate([self._out, done])
#
#             self._acc = np.concatenate([self._acc[self.H:],
#                                         np.zeros(self.H, dtype=np.float32)])
#             self._nrm = np.concatenate([self._nrm[self.H:],
#                                         np.zeros(self.H, dtype=np.float32)])
#             self._in = self._in[self.H:]
#
#         # Emit exactly as many samples as came in (front-padding with silence
#         # only during the initial one-frame warm-up).
#         n = mono_int16.shape[0]
#         if self._out.shape[0] >= n:
#             out, self._out = self._out[:n], self._out[n:]
#         else:
#             pad = np.zeros(n - self._out.shape[0], dtype=np.float32)
#             out = np.concatenate([pad, self._out])
#             self._out = np.zeros(0, dtype=np.float32)
#         return np.clip(out, -32768, 32767).astype(np.int16)


# ─── Pipeline stages ─────────────────────────────────────────────────────────
async def listen_audio():
    loop = asyncio.get_running_loop()
    if _audio_enhancer is not None:
        _audio_enhancer.reset()

    # NOISE SUPPRESSION DISABLED (v10)
    # # One denoiser per session (re-learns the noise floor on each reconnect).
    # denoiser = _SpectralDenoiser() if NOISE_SUPPRESSION_ENABLED else None
    # if denoiser is not None:
    #     print(f"[NOISE] Static suppression ACTIVE "
    #           f"(alpha={NS_OVERSUBTRACT}, floor={NS_SPECTRAL_FLOOR}, "
    #           f"gate={'on' if NOISE_GATE_ENABLED else 'off'}, hp={NS_HP_CUTOFF_HZ:.0f}Hz)",
    #           flush=True)

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"[MIC STATUS] {status}", flush=True)

        # Downmix stereo to mono for Gemini if hardware requires 2 channels
        if INPUT_CHANNELS > 1:
            mono_data = np.mean(indata, axis=1).astype(np.int16)
        else:
            mono_data = indata.flatten().astype(np.int16)
        raw_bytes = mono_data.tobytes()

        # ── Record RAW audio for voice biomarker analysis ──
        # Keep it un-denoised: spectral subtraction would distort the very
        # jitter/shimmer/prosody features the analyzer measures.
        try:
            _audio_record_queue.put_nowait(raw_bytes)
        except queue.Full:
            pass

        # NOISE SUPPRESSION DISABLED (v10) — Gemini gets the raw mic stream.
        # # ── Static / crackle suppression on the Gemini-bound stream only ──
        # if denoiser is not None:
        #     clean_mono = denoiser.process(mono_data)
        #     send_bytes = clean_mono.tobytes()
        # else:
        #     clean_mono = mono_data
        #     send_bytes = raw_bytes
        clean_mono = _audio_enhancer.process(mono_data, time_info) if _audio_enhancer is not None else mono_data
        send_bytes = clean_mono.tobytes()
        if _waiting_cue is not None:
            _waiting_cue.microphone(send_bytes, INTERRUPT_RMS_THRESHOLD)

        # ── Volume check (on the CLEANED signal, so the loud-mic / VAD gate
        #    tracks real speech instead of amplified static) ──
        rms = np.sqrt(np.mean(clean_mono.astype(np.float32) ** 2))
        peak = int(np.max(np.abs(clean_mono))) if clean_mono.size else 0

        # Track recent loud mic frames so the initiation monitor can avoid
        # opening a conversation while the user is mid-utterance.
        if rms > INTERRUPT_RMS_THRESHOLD:
            with _initiation_ts_lock:
                global _last_loud_mic_ts
                _last_loud_mic_ts = time.monotonic()
        if not hasattr(audio_callback, '_count'):
            audio_callback._count = 0
        audio_callback._count += 1
        if audio_callback._count % 50 == 0:
            print(f"[MIC LEVEL] rms={rms:.0f}  peak={peak}  (max=32767)", flush=True)

        loop.call_soon_threadsafe(
            audio_queue_mic.put_nowait,
            {
                "data": send_bytes,
                "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                "ts": time.perf_counter(),
            },
        )

    stream = sd.InputStream(
        samplerate=SEND_SAMPLE_RATE,
        channels=INPUT_CHANNELS,
        dtype="int16",
        blocksize=CHUNK_SIZE,
        callback=audio_callback,
    )
    if _audio_enhancer is not None:
        _audio_enhancer.input_latency_ms = float(stream.latency) * 1000
    with stream:
        while True:
            await asyncio.sleep(1)

async def send_audio_realtime(session):
    """Sends all mic audio to Gemini with batch-drain. No VAD filtering."""
    global _last_mic_send_ts
    while True:
        msg = await audio_queue_mic.get()
        batch = [msg]
        while not audio_queue_mic.empty():
            try:
                batch.append(audio_queue_mic.get_nowait())
            except asyncio.QueueEmpty:
                break

        for msg in batch:
            queue_wait_ms = (
                time.perf_counter() - msg.get("ts", time.perf_counter())
            ) * 1000
            tracker_mic_queue.record(queue_wait_ms)
            if _audio_enhancer is not None and _audio_enhancer.stale(queue_wait_ms):
                continue

            with _playback_lock:
                buffer_empty = len(_playback_buffer) == 0
            if not buffer_empty and not (_audio_enhancer is not None and _audio_enhancer.full_duplex):
                continue

            t1 = time.perf_counter()
            try:
                async with asyncio.timeout(MIC_SEND_TIMEOUT_S):
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=msg["data"], mime_type=msg["mime_type"]
                        )
                    )
            except Exception as e:
                # Mic audio can't reach Gemini: end the session so run() reconnects.
                print(f"Error sending audio: {e!r}")
                monitor.emit("reconnecting", "Gemini send connection needs recovery")
                raise
            send_ms = (time.perf_counter() - t1) * 1000
            tracker_send_audio.record(send_ms)
            _last_mic_send_ts = time.perf_counter()

async def send_video_realtime(session):
    """Reads shared frames from the fall detection thread and sends to Gemini every 3s."""
    # Wait for fall detection thread to start capturing
    for _ in range(50):
        with _latest_frame_lock:
            if _latest_frame is not None:
                break
        await asyncio.sleep(0.1)

    with _latest_frame_lock:
        if _latest_frame is None:
            print("[VIDEO] No camera frames available — video disabled.")
            return

    print("[VIDEO] Sending shared camera frames to Gemini (1 frame / 3s).")

    while True:
        await asyncio.sleep(3.0)
        t0 = time.perf_counter()

        with _latest_frame_lock:
            frame = _latest_frame
        if frame is None:
            continue

        # Resize to 320x240 for Gemini (fall detection runs at 640x480)
        small = cv2.resize(frame, (320, 240))
        _, buffer = cv2.imencode(
            ".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 50]
        )
        jpg_bytes = buffer.tobytes()
        encode_ms = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        try:
            await session.send_realtime_input(
                video=types.Blob(data=jpg_bytes, mime_type="image/jpeg")
            )
        except Exception as e:
            print(f"[VIDEO] Error sending frame: {e}")
            continue
        send_ms = (time.perf_counter() - t1) * 1000
        tracker_send_video.record(encode_ms + send_ms)

async def fall_alert_monitor(session):
    """Watches the fall detection event and injects an alert into the Gemini session."""
    global _fall_held_note
    from features.conversational_ability.engage import engagement
    loop = asyncio.get_running_loop()
    while True:
        triggered = await loop.run_in_executor(
            None, _fall_detected_event.wait, 2.0
        )
        if not triggered:
            continue
        _fall_detected_event.clear()
        engagement.engage('possible fall')
        # Radio, recordings and audiobooks withhold the mic from Gemini: pause them so they can answer.
        # A retry finds playback already paused (nothing new to say), so it keeps the first note.
        from features.toolkits import audiobooks
        held = audiobooks.hold_for_answer('possible fall') or _fall_held_note
        _fall_held_note = ""

        print("[FALL] Injecting fall alert into Gemini session...", flush=True)
        try:
            await session.send_client_content(
                turns={
                    "role": "user",
                    "parts": [{
                        "text": (
                            "[FALL ALERT] The camera has detected that the user "
                            "may have just fallen down. Please immediately check "
                            "on them — ask if they are okay and if they need help."
                        ) + held
                    }],
                },
                turn_complete=True,
            )
        except BaseException as e:
            # Not delivered (a reconnect only resends what was): ask again on the next try.
            _fall_detected_event.set()
            _fall_held_note = held
            if not isinstance(e, Exception):
                raise
            print(f"[FALL] Failed to inject alert: {e}; asking again shortly")
            await asyncio.sleep(2)

# ─── Conversation initiation ─────────────────────────────────────────────────
def _safe_to_initiate(now: float) -> bool:
    """Gating: safe to open a conversation only when the robot is not already
    speaking, the playback buffer is drained, and the user is not mid-utterance."""
    with _gemini_speaking_lock:
        speaking = _gemini_speaking
    with _playback_lock:
        buffered = len(_playback_buffer) > 0
    with _initiation_ts_lock:
        loud = _last_loud_mic_ts
    user_recent = (now - loud) < INITIATION_USER_SILENCE_GATE
    return not speaking and not buffered and not user_recent


def _initiation_memory_hint():
    """A few stored memories for the opener. Runs on a worker thread: the lookup
    embeds the query and reads Chroma, which would hold up the event loop."""
    memory_hint = ""
    if _memory_embedder is not None:
        try:
            with _recent_user_words_lock:
                query = " ".join(_recent_user_words[-MEMORY_WORD_WINDOW:])
            docs = []
            if query.strip():
                results = _memory_embedder.search(query, n_results=MEMORY_TOP_K)
                docs = [r["document"] for r in results
                        if r["distance"] <= MEMORY_MAX_DISTANCE]
            if not docs:
                # No recent context to go on — pull a few random recent memories.
                all_docs = _newest_memories()
                if all_docs:
                    docs = random.sample(all_docs, min(3, len(all_docs)))
            if docs:
                memory_hint = (
                    "\nThings you remember about them (optional, use only if it "
                    "feels natural):\n" + "\n".join(f"- {d}" for d in docs)
                )
        except Exception as e:
            print(f"[INIT] Memory hint failed: {e}")
    return memory_hint


async def _fire_initiation(session):
    """Inject a prompt telling the robot to open a conversation. Supplies a few
    stored memories so it can pick a context-dependent or generic opener."""
    memory_hint = await asyncio.get_running_loop().run_in_executor(None, _initiation_memory_hint)
    if not _safe_to_initiate(time.monotonic()):
        print("[INIT] The user spoke during the memory lookup; no opener.", flush=True)
        return

    prompt = (
        "[INITIATE CONVERSATION] The user has been quietly present for a while "
        "and you have not spoken in some time. Gently open a conversation on "
        "your own — a warm, short opener or a light question. You may reference "
        "something you remember about them if it fits, or just make friendly "
        "small talk. Keep it brief and human; do not mention that you were "
        "prompted." + memory_hint
    )

    print("[INIT] Initiating conversation with the user...", flush=True)
    try:
        await session.send_client_content(
            turns={"role": "user", "parts": [{"text": prompt}]},
            turn_complete=True,
        )
        _notify_webapp("/api/transcript",
                       {"speaker": "system", "text": "[Robot initiated conversation]"})
    except Exception as e:
        print(f"[INIT] Failed to initiate: {e}")


async def initiation_monitor(session):
    """Lets the robot open a conversation on its own.

    Enters 'initiation mode' after IDLE_THRESHOLD_SECONDS of robot silence (and,
    if RESET_IDLE_ON_USER_SPEECH, user silence too). While the user's face is
    visible it evaluates the precomputed coin table once per tick; a fired coin
    latches a pending intent that emits at the next gate-clear moment. The
    face-presence clock resets after FACE_ABSENCE_RESET_SECONDS of no face."""
    global _last_robot_speech_ts

    clock_start = None        # monotonic time the face-presence clock started (or None)
    last_eval_ts = 0.0        # last time a coin was rolled
    pending_fire = False      # coin fired, waiting for a safe moment to speak

    while not _shutdown_event.is_set():
        await asyncio.sleep(1.0)
        now = time.monotonic()

        # ── Idle detection ──
        with _initiation_ts_lock:
            idle_since = _last_robot_speech_ts
            if RESET_IDLE_ON_USER_SPEECH:
                idle_since = max(idle_since, _last_user_speech_ts)
        if (now - idle_since) < IDLE_THRESHOLD_SECONDS:
            # Not idle → not in initiation mode. Reset the machine.
            clock_start = None
            pending_fire = False
            continue

        # ── Face-presence clock (only accrues in initiation mode) ──
        with _face_state_lock:
            face_seen = _face_last_seen_ts
        gap = (now - face_seen) if face_seen > 0 else 1e9
        currently_visible = gap < FACE_FRESH_SECONDS

        if gap > FACE_ABSENCE_RESET_SECONDS:
            clock_start = None            # gone too long → reset clock + intent
            pending_fire = False
        elif clock_start is None and currently_visible:
            clock_start = now             # face just (re)appeared → start the clock

        if clock_start is None:
            continue                      # no face yet in this initiation window

        face_clock_t = now - clock_start

        # ── Pending fire: emit as soon as the face is up and gating clears ──
        if pending_fire:
            if currently_visible and _safe_to_initiate(now):
                await _fire_initiation(session)
                with _initiation_ts_lock:
                    _last_robot_speech_ts = now   # arm the next idle window
                clock_start = None
                pending_fire = False
            continue

        # ── Roll a fresh coin once per tick, only while the face is visible ──
        if currently_visible and (now - last_eval_ts) >= INITIATION_TICK_SECONDS:
            last_eval_ts = now
            idx = min(int(face_clock_t // INITIATION_TICK_SECONDS),
                      len(_HAZARD_TABLE) - 1)
            h = _HAZARD_TABLE[idx]
            if random.random() < h:
                print(f"[INIT] Coin fired at t={face_clock_t:.0f}s of face "
                      f"presence (h={h:.3f})", flush=True)
                pending_fire = True
                if _safe_to_initiate(now):
                    await _fire_initiation(session)
                    with _initiation_ts_lock:
                        _last_robot_speech_ts = now
                    clock_start = None
                    pending_fire = False


class _TurnTranscript:
    """
    Collects one speaker's transcription fragments and posts them as a single
    utterance. Fragments arrive word by word, sometimes mid-word ("A" + "mber"),
    and carry their own spacing, so they are joined exactly as received.
    """

    def __init__(self, speaker):
        self.speaker = speaker
        self._parts = []

    def add(self, text):
        self._parts.append(text)

    def flush(self):
        text = " ".join("".join(self._parts).split())
        self._parts.clear()
        if text:
            _notify_webapp("/api/transcript", {"speaker": self.speaker, "text": text})


async def receive_audio(session):
    """Receives audio from Gemini, upsamples 24kHz -> 48kHz, appends to buffer.
    Also captures input_transcription and output_transcription side-channel data.
    On each completed user turn, retrieves relevant memories and injects them."""
    global _last_mic_send_ts, _last_robot_speech_ts, _last_user_speech_ts, _gemini_speaking
    _is_new_turn = True
    _reply_audio_started = False

    _memory_injected_this_turn = False
    _current_turn_fragments = []
    # Future for background memory retrieval started during transcription
    _retrieval_future: asyncio.Future = None
    user_said = _TurnTranscript("user")
    gemini_said = _TurnTranscript("gemini")

    receive_failures = 0
    while True:
        try:
            async for response in session.receive():
                receive_failures = 0
                t0 = time.perf_counter()
                received_at = monitor.timing.now()
                if response.session_resumption_update or response.go_away:
                    from features.conversational_ability.session import on_server_message
                    on_server_message(response)
                if response.tool_call:
                    _address_gate.tool_call()
                    await public_toolkit.handle_tool_call(session, response.tool_call)
                server_content = response.server_content
                if server_content is None:
                    continue

                if server_content.interrupted:
                    # Cancel before examining any co-delivered output; retain new user input below.
                    _flush_playback()
                    monitor.timing.complete_reply(interrupted=True)
                    _address_gate.end(interrupted=True)
                    monitor.finish_transcripts("ember")
                    monitor.emit("listening", "Reply interrupted; listening")
                    gemini_said.flush()
                    with _gemini_speaking_lock:
                        _gemini_speaking = False
                    _is_new_turn = True
                    _reply_audio_started = False
                    _memory_injected_this_turn = False
                    _current_turn_fragments.clear()
                    _retrieval_future = None

                # ── Input transcription (what the USER said) ──
                if server_content.input_transcription:
                    text = server_content.input_transcription.text
                    monitor.transcribe("user", text)
                    if text and text.strip():
                        monitor.emit("thinking", "User speech received; waiting for Gemini")
                        _transcript_add("user", text)
                        print(f"[USER] {text.strip()}", flush=True)
                        gemini_said.flush()  # the user is speaking, so Gemini's utterance is over
                        user_said.add(text)
                        for held in _address_gate.heard(text):
                            held()  # a held reply turned out to be meant for Ember

                        # Feed the idle timer: the user was just heard.
                        with _initiation_ts_lock:
                            _last_user_speech_ts = time.monotonic()

                        # Accumulate words for memory retrieval
                        _current_turn_fragments.append(text.strip())
                        with _recent_user_words_lock:
                            _recent_user_words.extend(text.strip().split())
                            if len(_recent_user_words) > MEMORY_WORD_WINDOW:
                                _recent_user_words[:] = _recent_user_words[-MEMORY_WORD_WINDOW:]

                        # Start retrieval in background on first fragment so the
                        # result is ready before model_turn fires.
                        if not _memory_injected_this_turn and _retrieval_future is None and _memory_embedder is not None:
                            with _recent_user_words_lock:
                                query = " ".join(_recent_user_words[-MEMORY_WORD_WINDOW:])
                            if query.strip():
                                loop = asyncio.get_running_loop()
                                _retrieval_future = loop.run_in_executor(
                                    None, _retrieve_memories, query
                                )

                if server_content.interrupted:
                    continue  # Audio/text on this cancellation message belongs to the cancelled reply.

                # ── Output transcription (what GEMINI said) ──
                if server_content.output_transcription:
                    text = server_content.output_transcription.text
                    if text and text.strip():
                        monitor.timing.received_text(at=received_at)

                    def show_reply_text(text=text):
                        monitor.transcribe("ember", text)
                        if text and text.strip():
                            _transcript_add("gemini", text)
                            print(f"[GEMINI TXT] {text.strip()}", flush=True)
                            user_said.flush()  # Gemini is answering, so the user's utterance is over
                            gemini_said.add(text)
                    _address_gate.reply(show_reply_text, text)

                model_turn = server_content.model_turn
                if model_turn:
                    # Inject memory before processing any audio from this turn.
                    # Retrieval was started during transcription, so it should
                    # already be done — await with a short timeout as a safety net.
                    if not _memory_injected_this_turn and _retrieval_future is not None:
                        _memory_injected_this_turn = True
                        try:
                            memory_context = await asyncio.wait_for(
                                asyncio.ensure_future(_retrieval_future), timeout=0.15
                            )
                            if memory_context:
                                await session.send_client_content(
                                    turns={
                                        "role": "user",
                                        "parts": [{"text": memory_context}],
                                    },
                                    turn_complete=False,
                                )
                        except asyncio.TimeoutError:
                            print("[MEMORY] Retrieval timed out — skipping this turn")
                        except Exception as e:
                            print(f"[MEMORY] Failed to inject context: {e}")
                        _retrieval_future = None

                    for part in model_turn.parts:
                        if part.text:
                            print(f"[GEMINI] {part.text}", flush=True)
                        if (
                            part.inline_data
                            and part.inline_data.mime_type.startswith(
                                "audio/pcm"
                            )
                        ):
                            timing_id = monitor.timing.received_audio(at=received_at)
                            def play_reply_audio(data=part.inline_data.data, new_turn=not _reply_audio_started,
                                                 timing_id=timing_id):
                                global _last_robot_speech_ts, _gemini_speaking
                                monitor.emit("speaking", "Gemini is producing reply audio")
                                with _gemini_speaking_lock:
                                    _gemini_speaking = True

                                # --- AUDIO UPSAMPLING MAGIC (24kHz -> 48kHz) ---
                                audio_array = np.frombuffer(data, dtype=np.int16)
                                upsampled_array = np.repeat(audio_array, 2)
                                upsampled_bytes = upsampled_array.tobytes()
                                _append_playback(upsampled_bytes, new_turn=new_turn,
                                                 timing_id=timing_id if new_turn else None)
                                # -----------------------------------------------
                                _address_gate.spoke(time.monotonic() + len(_playback_buffer)
                                                    / (RECEIVE_SAMPLE_RATE * 2 * OUTPUT_CHANNELS))

                                # Robot is speaking → reset the idle timer so
                                # initiation mode only arms after real silence.
                                with _initiation_ts_lock:
                                    _last_robot_speech_ts = time.monotonic()
                            _reply_audio_started = True
                            _address_gate.reply(play_reply_audio)

                            recv_ms = (time.perf_counter() - t0) * 1000
                            tracker_receive.record(recv_ms)

                            if _is_new_turn and _last_mic_send_ts > 0:
                                first_ms = (
                                    time.perf_counter() - _last_mic_send_ts
                                ) * 1000
                                tracker_first_audio.record(first_ms)
                                _is_new_turn = False

                            if _last_mic_send_ts > 0:
                                rt_ms = (
                                    time.perf_counter() - _last_mic_send_ts
                                ) * 1000
                                tracker_roundtrip.record(rt_ms)

                if server_content.turn_complete:
                    monitor.timing.complete_reply()
                    _address_gate.end()
                    monitor.finish_transcripts()
                    monitor.emit("listening", "Reply complete; listening (buffered audio may still play)")
                    user_said.flush()
                    gemini_said.flush()
                    with _gemini_speaking_lock:
                        _gemini_speaking = False
                    _is_new_turn = True
                    _reply_audio_started = False
                    _memory_injected_this_turn = False
                    _current_turn_fragments.clear()
                    _retrieval_future = None

        except asyncio.CancelledError:
            # The send side saw the disconnect first: still post what was said so far.
            user_said.flush()
            gemini_said.flush()
            raise
        except Exception as e:
            print(f"Receive error: {e}")
            monitor.emit("reconnecting", "Gemini receive connection needs recovery")
            user_said.flush()
            gemini_said.flush()
            # WebSocket session died — re-raise so TaskGroup can reconnect. An error
            # handling one message isn't a disconnect, unless nothing arrives after it.
            receive_failures += 1
            if connection_lost(e) or receive_failures >= RECEIVE_FAILURES_BEFORE_RECONNECT:
                raise
            await asyncio.sleep(0.5)
            continue

# ─── Memory saving: Summarise & Embed ─────────────────────────────────────────
# Runs during uptime (_memory_saver_thread) and, for what is left, on a clean
# stop (_save_memories_on_stop). Each save sends only the conversation since
# the previous one, plus its last few lines as context, to SUMMARY_MODEL.

# The raw text behind saved memories, appended to one file per run.
_TRANSCRIPT_FILE = os.path.join(TRANSCRIPT_DIR, f"transcript_{time.strftime('%Y%m%d_%H%M%S')}.txt")
_memory_save_lock = threading.Lock()  # one save at a time: the saver thread or the stop path
_summary_embedder = None  # store loaded for saving when retrieval hasn't loaded one


def _memory_store():
    """The memory store to save into, or None while it can't be loaded (memory
    isn't installed, or its model is still downloading on a first start)."""
    global _summary_embedder
    if _memory_embedder is not None:
        _summary_embedder = None  # retrieval's store loaded after all (first-start download)
        return _memory_embedder
    # On a first start the download thread loads retrieval's store once the model is in.
    downloading = any(t.name == "memory-download" for t in threading.enumerate())
    if _summary_embedder is None and not downloading and model_present(ONNX_MODEL_DIR):
        try:
            _summary_embedder = SemanticEmbedder(
                model_dir=ONNX_MODEL_DIR,
                chroma_dir=CHROMA_DIR,
                collection_name="user_memories",
            )
        except Exception as e:
            print(f"[EMBED] Could not load the memory store: {e}")
    return _summary_embedder


def _format_transcript(entries):
    names = {"user": "User", "gemini": "Gemini"}
    return "\n".join(f"{names[speaker]}: {' '.join(text.split())}" for speaker, text in entries)


def _generate_summary(prompt, timeout):
    """SUMMARY_MODEL's reply text. Raises TimeoutError after `timeout` seconds: the
    call runs on a daemon thread, so a stalled connection (DNS, Wi-Fi) can't hold
    up a stop, and the HTTP request itself has the same timeout."""
    reply = {}

    def call():
        try:
            client = genai.Client(
                api_key=API_KEY,
                http_options={"api_version": "v1alpha", "timeout": int(timeout * 1000)},
            )
            reply["text"] = client.models.generate_content(model=SUMMARY_MODEL, contents=prompt).text
        except Exception as e:
            reply["error"] = e
    worker = threading.Thread(target=call, name="memory-summary", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"no reply within {timeout:.0f} s")
    if "error" in reply:
        raise reply["error"]
    return reply.get("text")


def _keep_transcript_unsaved():
    """A save failed: its utterances stay unsaved for the next try."""
    global _transcript_claimed
    with _transcript_lock:
        _transcript_claimed = _transcript_saved


def _mark_transcript_saved(end):
    """Entries [0:end) are stored: keep the last few, shortened, as context for
    the next summary and drop the rest."""
    global _transcript_saved, _transcript_claimed, _transcript_unsaved_chars
    with _transcript_lock:
        _transcript_unsaved_chars -= sum(len(text) for _, text in _transcript[_transcript_saved:end])
        drop = max(0, end - MEMORY_CONTEXT_UTTERANCES)
        del _transcript[:drop]
        _transcript_saved = _transcript_claimed = end - drop
        for entry in _transcript[:_transcript_saved]:
            entry[1] = entry[1][-MEMORY_CONTEXT_CHARS:]


def _memory_save_due(now):
    """Why the conversation since the last save should be summarised now, or None."""
    with _transcript_lock:
        unsaved = _transcript[_transcript_saved:]
        chars, last = _transcript_unsaved_chars, _transcript_last_ts
    if not unsaved:
        return None
    turns = sum(1 for speaker, _ in unsaved if speaker == "user")
    if turns >= MEMORY_SAVE_USER_TURNS:
        return f"{turns} user turns"
    if chars >= MEMORY_SAVE_CHARS:
        return f"{chars} characters"
    if now - last >= MEMORY_SAVE_IDLE_SECONDS:
        return f"quiet for {now - last:.0f} s"
    return None


def _summarise_and_embed(deadline):
    """Summarise the conversation since the last save with SUMMARY_MODEL (reply due
    by `deadline`, a time.monotonic() value), store the summary lines in ChromaDB
    and trim what was saved. Returns False if it failed; the utterances then stay
    unsaved for a later try."""
    global _transcript_claimed
    with _transcript_lock:
        start = _transcript_saved
        end = _transcript_claimed = len(_transcript)
        context = [tuple(entry) for entry in _transcript[:start]]
        new = [tuple(entry) for entry in _transcript[start:end]]
    try:
        return _summarise_claimed(context, new, end, deadline)
    except Exception as e:
        print(f"[SUMMARY] Saving memories failed: {e}")
        _keep_transcript_unsaved()  # harmless if they were saved before the error
        return False


def _summarise_claimed(context, new, end, deadline):
    """_summarise_and_embed's work on the claimed utterances `new` (up to entry `end`)."""
    user_count = sum(1 for speaker, _ in new if speaker == "user")
    if not user_count:
        # Nothing new, or only Ember spoke (an opener nobody answered).
        print("[SUMMARY] Nothing new from the user since the last save — skipping summarisation.")
        _mark_transcript_saved(end)
        return True

    # Only send the transcript to Gemini if the summary can be stored.
    embedder = _memory_store()
    if embedder is None:
        print("[SUMMARY] Memory store not available (memory packages or model missing) — "
              "not summarising.")
        _keep_transcript_unsaved()
        return False

    full_transcript = _format_transcript(new)
    print(f"[SUMMARY] Summarising {user_count} user and {len(new) - user_count} gemini "
          f"utterances since the last save")

    # ── Call Gemini Flash (non-streaming, sync) ──
    try:
        earlier = ""
        if context:
            earlier = (
                "The transcript continues a conversation whose earlier part was "
                "already summarised. Its last lines are below for context only; "
                "extract nothing from them.\n"
                "--- EARLIER CONTEXT START ---\n"
                f"{_format_transcript(context)}\n"
                "--- EARLIER CONTEXT END ---\n\n"
            )
        prompt = (
            "You are a memory extraction system.  Below is a transcript of "
            "a voice conversation between a user and an AI assistant.  Your "
            "job is to extract concise yet thorough summary lines of "
            "*important information about the user* that would be worth "
            "remembering for future conversations.\n\n"
            "Focus on:\n"
            "- Personal facts (name, age, location, occupation, family)\n"
            "- Preferences and opinions\n"
            "- Goals, plans, and aspirations\n"
            "- Problems or concerns they mentioned\n"
            "- Emotional states and what triggered them\n"
            "- Specific requests or topics they care about\n"
            "- Relationships and people they mentioned\n\n"
            "Output ONLY the summary lines, one per line.  No numbering, no "
            "bullets, no preamble.  Each line should be a self-contained fact "
            "or observation.  If there is nothing meaningful to extract, "
            "output exactly: NOTHING_TO_REMEMBER\n\n"
            + earlier +
            "--- TRANSCRIPT START ---\n"
            f"{full_transcript}\n"
            "--- TRANSCRIPT END ---"
        )

        timeout = deadline - time.monotonic()
        if timeout < 1.0:
            raise TimeoutError("no time left")
        # No text (e.g. a blocked reply) counts as nothing to remember, so one
        # chunk can't stop every later save.
        summary_text = (_generate_summary(prompt, timeout) or "").strip()
    except Exception as e:
        print(f"[SUMMARY] Gemini summarisation failed: {e}")
        _keep_transcript_unsaved()
        return False

    if not summary_text or summary_text == "NOTHING_TO_REMEMBER":
        print("[SUMMARY] Nothing worth remembering was found.")
        _mark_transcript_saved(end)
        return True

    summary_lines = [
        line.strip() for line in summary_text.splitlines() if line.strip()
    ]
    print(f"[SUMMARY] Extracted {len(summary_lines)} memory lines:")
    for i, line in enumerate(summary_lines):
        print(f"  {i+1}. {line}")

    # ── Embed into ChromaDB via SemanticEmbedder ──
    try:
        print("\n[EMBED] Saving to ChromaDB …")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        batch = uuid.uuid4().hex[:8]  # two saves can fall in the same second
        ids = [f"memory_{timestamp}_{batch}_{i}" for i in range(len(summary_lines))]
        metadatas = [
            {
                "source": "conversation_summary",
                "timestamp": timestamp,
                "line_index": str(i),
            }
            for i in range(len(summary_lines))
        ]

        embedder.save(summary_lines, ids=ids, metadatas=metadatas)
        print(f"[EMBED] ✓ Saved {len(summary_lines)} memories to ChromaDB")
    except Exception as e:
        print(f"[EMBED] Embedding failed: {e}")
        _keep_transcript_unsaved()
        return False
    _mark_transcript_saved(end)

    # ── Also append the raw transcript to this run's file for reference ──
    try:
        with open(_TRANSCRIPT_FILE, "a") as f:
            f.write(full_transcript + "\n")
        print(f"[TRANSCRIPT] Raw transcript saved to {_TRANSCRIPT_FILE}")
    except Exception as e:
        print(f"[TRANSCRIPT] Could not save transcript file: {e}")
    return True


def _memory_saver_thread():
    """Saves memories while Ember runs (see MEMORY_SAVE_*), off the event loop."""
    retry_at = 0.0
    while not _shutdown_event.wait(5.0):
        try:
            now = time.monotonic()
            reason = _memory_save_due(now) if now >= retry_at else None
            if reason is None:
                continue
            with _memory_save_lock:
                if _shutdown_event.is_set():
                    break  # the stop path saves what is left
                print(f"[MEMORY] Saving the conversation so far ({reason})")
                if not _summarise_and_embed(time.monotonic() + MEMORY_SUMMARY_TIMEOUT):
                    retry_at = time.monotonic() + MEMORY_SAVE_RETRY_SECONDS
        except Exception as e:  # one bad save must not end saving for the run
            print(f"[MEMORY] Saver error: {e}")
            retry_at = time.monotonic() + MEMORY_SAVE_RETRY_SECONDS


def _save_memories_on_stop(deadline):
    """Stop path: save what the saver thread hasn't, finishing by `deadline` (a
    time.monotonic() value). A save already in progress is waited for first."""
    if not _memory_save_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        print("[SUMMARY] Out of time: a save was still running, so the conversation "
              "since the last save was not saved.")
        return
    try:
        # A second is left to embed and store the summary lines.
        _summarise_and_embed(deadline - 1.0)
    finally:
        _memory_save_lock.release()


def _run_voice_analysis():
    """Post-session: concatenate recorded audio, run biomarker analysis,
    save results, check for alerts, notify webapp, delete audio files."""
    from voice_analyzer import VoiceAnalyzer

    print("\n" + "=" * 70)
    print("RUNNING VOICE BIOMARKER ANALYSIS")
    print("=" * 70)

    analyzer = VoiceAnalyzer()

    session_wav = os.path.join(DATA_DIR, f"session_{time.strftime('%Y%m%d_%H%M%S')}.wav")
    merged = VoiceAnalyzer.concatenate_wavs(DAY_UTTERANCE_DIR, session_wav)
    if not merged:
        print("[VOICE] No audio recorded — skipping analysis.")
        return

    with _transcript_lock:
        user_lines = list(_transcript_user)
    transcript = " ".join(user_lines)

    if not transcript.strip():
        print("[VOICE] No transcript — running acoustic analysis only.")
        transcript = ""

    try:
        results = analyzer.analyze_session(session_wav, transcript)
        print(f"[VOICE] Analysis complete for session {results['session_id']}")

        summary = analyzer.save_session_results(results)

        print(f"[VOICE] Sessions recorded: {summary['session_count']}")
        print(f"[VOICE] Baseline ready: {summary['baseline_ready']}")

        if summary.get("deviations"):
            print(f"[VOICE] Deviations detected: {len(summary['deviations'])}")
            for d in summary["deviations"]:
                print(f"  - {d['metric']}: {d['direction']} (z={d['z_score']:.2f})")

        if summary.get("alerts"):
            print("[VOICE] *** CLINICAL PATTERN ALERTS ***")
            for a in summary["alerts"]:
                print(f"  - {a['name']} ({a['matching_indicators']}/{a['total_indicators']} indicators)")

        _notify_webapp("/api/voice-analysis", {
            "session": results,
            "summary": summary,
        })

    except Exception as e:
        print(f"[VOICE] Analysis failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            os.remove(session_wav)
        except OSError:
            pass


# ─── Main ─────────────────────────────────────────────────────────────────────
async def run():
    from features.conversational_ability import session as session_state
    global _last_robot_speech_ts, _stop_started_ts
    monitor.start(lambda: {"playback_bytes": len(_playback_buffer), "gemini_generating_audio": _gemini_speaking, "audio_input": dict(_audio_enhancer.metrics) if _audio_enhancer is not None else None})
    public_toolkit.prewarm()
    client = genai.Client(
        api_key=API_KEY, http_options={"api_version": "v1alpha"}
    )
    quick_failures = 0
    while True:
        connected_at = time.monotonic()
        connected = False
        try:
            monitor.finish_transcripts()
            monitor.emit("connecting", "Connecting to Gemini Live")
            print(f"Connecting to {MODEL}...")
            _boot_status("connecting", f"Connecting to Gemini model...")
            async with client.aio.live.connect(
                model=MODEL, config=_build_config()
            ) as live_session:
                connected = True
                connected_at = time.monotonic()
                live_session = _address_gate.watch(live_session)  # notes Ember's own messages
                print("Connected. System ready.")
                monitor.emit("listening", "Connected to Gemini; listening")
                _boot_status("ready", "Baymax is ready.", ready=True)
                print("=" * 70)
                print("No client-side VAD — all audio sent to Gemini")
                print("Interrupts handled server-side")
                print("Transcription: input + output (Gemini built-in)")
                mem_count = (
                    _memory_embedder._collection.count()
                    if _memory_embedder and _memory_embedder._collection
                    else 0
                )
                print(f"Memory retrieval: {'ACTIVE' if _memory_embedder else 'DISABLED'}"
                      f" ({mem_count} memories)" if MEMORY_RETRIEVAL_ENABLED
                      else "Memory retrieval: OFF (memory.retrieval_enabled = false in device.toml)")
                print(f"Fall detection:   ACTIVE (angle>{FALL_ANGLE_THRESHOLD:.0f}°, stay down {FALL_STAY_DOWN_SECONDS:.0f}s, {FALL_DETECTION_FPS} fps)"
                      if FALL_DETECTION_ENABLED else "Fall detection:   OFF (fall_detection.enabled = false in device.toml)")
                if not POSE_TRACKING_ENABLED:
                    print("Pose tracking:    OFF (camera.pose_tracking = false in device.toml; no face presence, so no initiation)")
                print(f"Random initiation: {'ACTIVE' if RANDOM_INITIATION_ENABLED else 'OFF'}"
                      + (f" (idle>{IDLE_THRESHOLD_SECONDS:.0f}s, sigmoid t0={F_MIDPOINT:.0f}s)"
                         if RANDOM_INITIATION_ENABLED else " (event-driven conversation only)"))
                print("=" * 70)
                # Start each session's idle window fresh from connection time.
                with _initiation_ts_lock:
                    _last_robot_speech_ts = time.monotonic()
                output_stream = start_output_stream()

                try:
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(_send_cut_off_prompts(live_session))
                        tg.create_task(listen_audio())
                        tg.create_task(send_audio_realtime(live_session))
                        tg.create_task(send_video_realtime(live_session))
                        tg.create_task(receive_audio(live_session))
                        tg.create_task(fall_alert_monitor(live_session))
                        if RANDOM_INITIATION_ENABLED:
                            tg.create_task(initiation_monitor(live_session))
                        # MVP session tasks: reminder delivery, greetings, audiobook notes.
                        tg.create_task(fall_worker.alert_monitor(live_session, _shutdown_event))
                        tg.create_task(monitor_queues(interval=3.0))
                except asyncio.CancelledError:
                    # Ctrl+C cancels this task from outside. Swallowing that
                    # made the loop reconnect, so the shutdown summary never
                    # ran; re-raise it so asyncio.run ends. It raises
                    # KeyboardInterrupt, or CancelledError after a reconnect on
                    # Python 3.11/3.12: TaskGroup leaves this task's cancel count up.
                    if asyncio.current_task().cancelling():
                        # Stopping: the stop's time limit runs from here, and
                        # no new memory save starts.
                        _stop_started_ts = time.monotonic()
                        _shutdown_event.set()
                        raise
                finally:
                    _abandon_reply()
                    _abort_connection(live_session)
                    output_stream.stop()
                    output_stream.close()
        except Exception as e:
            if not connected and session_state.connection_not_started(e):
                session_state.not_tried()
            delay, quick_failures = session_state.retry_plan(e, time.monotonic() - connected_at, quick_failures)
            print(f"connection failed {e}. retrying...")
            _boot_status("connecting", f"Connection failed: {e}", error=True)
            if delay is not None:
                await asyncio.sleep(delay)
                continue
            # Device/unknown errors retain the existing reset and release delays.
            await asyncio.sleep(2)
            # Reset PortAudio to recover from ALSA/PipeWire device errors
            print("[AUDIO] Resetting PortAudio state...")
            try:
                await asyncio.to_thread(sd._terminate)
            except Exception:
                pass
            await asyncio.sleep(5)
            try:
                await asyncio.to_thread(sd._initialize)
                mic, spk = await asyncio.to_thread(get_default_device_id)
                sd.default.device = [mic, spk]
                print(f"[AUDIO] PortAudio reset. Input→{mic}, Output→{spk}")
            except Exception as reinit_err:
                print(f"[AUDIO] PortAudio reinit failed: {reinit_err}")

def _boot_status(stage: str, message: str, ready: bool = False, error: bool = False):
    _notify_webapp("/api/boot-status", {"stage": stage, "message": message, "ready": ready, "error": error})

if __name__ == "__main__":
    _boot_status("audio", "Configuring audio devices...")

    # ── Load memory embedder at startup ──
    if MEMORY_RETRIEVAL_ENABLED:
        _boot_status("memory", "Loading memory system...")
        if _memory_model_to_download():
            threading.Thread(target=_download_memory_model, name="memory-download", daemon=True).start()
            _boot_status("memory", "Downloading memory model in the background (first start)")
        else:
            _memory_embedder = _load_memory_embedder()
            if _memory_embedder is None:
                _boot_status("memory", "Memory system failed to load (non-fatal)", error=True)
    else:
        print("[MEMORY] Retrieval OFF (memory.retrieval_enabled = false in device.toml); session summaries are still saved.")
        _boot_status("memory", "Memory retrieval off (device.toml)")

    # ── Save memories during uptime, not only at a clean stop ──
    # Where memory isn't installed (the desktop profiles) nothing could store them.
    if not missing_packages():
        threading.Thread(target=_memory_saver_thread, name="memory-saver", daemon=True).start()

    # ── Download pose model if needed ──
    _boot_status("pose_model", "Loading pose model...")
    try:
        _ensure_pose_model()
    except Exception as e:
        _boot_status("pose_model", f"Pose model error: {e}", error=True)
        print(f"[BOOT] Pose model load failed: {e}")

    # ── Start fall detection in a dedicated thread ──
    _boot_status("camera", "Starting camera & pose tracking...")
    _fall_thread = threading.Thread(target=_fall_detection_thread, daemon=True)
    _fall_thread.start()
    print(f"[CAM] Camera/pose thread started (fall detection {'active' if FALL_DETECTION_ENABLED else 'OFF (device.toml)'}).")

    # ── Start MJPEG video stream server ──
    _boot_status("video_stream", "Starting video stream server...")
    _mjpeg_thread = threading.Thread(target=_start_mjpeg_server, args=(8080,), daemon=True)
    _mjpeg_thread.start()

    # ── Start audio recording thread ──
    _recording_thread = threading.Thread(target=_audio_writer_thread, daemon=True)
    _recording_thread.start()
    print("[RECORD] Audio recording thread started.")

    while True:
        try:
            asyncio.run(run())
        except (KeyboardInterrupt, asyncio.CancelledError):
            monitor.emit("stopping", "Stopping conversation and finishing local session analysis")
            print("\nInterrupted by user.")
            _shutdown_event.set()
            memory_deadline = (_stop_started_ts or time.monotonic()) + MEMORY_STOP_SECONDS

            # Stop recording thread gracefully
            _audio_record_queue.put(None)
            _recording_thread.join(timeout=5)

            # ── Print profiling summary ──
            print("\n" + "=" * 70)
            print("FINAL PROFILING SUMMARY")
            print("=" * 70)
            for t in [
                tracker_mic_queue,
                tracker_send_audio,
                tracker_send_video,
                tracker_receive,
                tracker_first_audio,
                tracker_roundtrip,
            ]:
                if t.samples:
                    t._print_summary()
                else:
                    print(f"[PROFILE] {t.name:.<30s} (no samples)")

            # ── Print the transcript not saved as memories yet ──
            with _transcript_lock:
                unsaved = [tuple(entry) for entry in _transcript[_transcript_saved:]]
            if unsaved:
                print("\n" + "=" * 70)
                print("CONVERSATION SINCE THE LAST MEMORY SAVE")
                print("=" * 70)
                for speaker, text in unsaved:
                    print(f"  {'[USER]  ' if speaker == 'user' else '[GEMINI]'} {' '.join(text.split())}")
            else:
                print("\n[TRANSCRIPT] No speech since the last memory save.")

            # ── Summarise the rest with Gemini Flash & embed into ChromaDB ──
            _save_memories_on_stop(memory_deadline)

            # ── Run voice biomarker analysis ──
            # The MVP launcher sets BAYMAX_VOICE_ANALYSIS=0: it delays every restart.
            if os.getenv("BAYMAX_VOICE_ANALYSIS", "1") != "0":
                _run_voice_analysis()
            else:
                print("[VOICE] Session analysis disabled (BAYMAX_VOICE_ANALYSIS=0).")

            break  # Exit the loop permanently
        except Exception as e:
            print(f"\n[!] CRITICAL SYSTEM OR HARDWARE ERROR: {e}")
            monitor.emit("reconnecting", "Runtime error; restarting connection")
            print("[!] Restarting the entire Gemini process in 5 seconds to recover...")
            import time
            time.sleep(5)
