# Baymax — AI Companion Robot

A real-time AI companion and health monitoring system built on Google Gemini Live API. Baymax listens, sees, and responds with low latency, while continuously monitoring for falls and tracking voice biomarkers over time.

## Architecture

Two processes run on startup:

| Process | File | Role |
|---|---|---|
| AI Core | `realtime_gemini_10.py` | Gemini Live session, audio/video, fall detection, memory |
| Web App | `baymax_app.py` | Flask server, mobile dashboard, email alerts |

### AI Core (`realtime_gemini_10.py`)

- **Realtime speech-to-speech** via Gemini Live API — no local ASR needed
- **Video streaming** — camera feed sent to Gemini for visual context (one frame every 3 s); annotated MJPEG stream on `127.0.0.1:8080`, shown in the dashboard behind its login
- **Fall detection** — MediaPipe Pose runs in a dedicated thread, shares the camera with Gemini. A fall must start from upright and the person must stay down for 2 s (`fall_detection/detector.py`); then Gemini checks on them and the web app emails an alert. Can be switched off per robot in `device.toml`
- **Semantic memory** — on each user turn, retrieves similar past memories from ChromaDB (via `semantic_embedder.py`) and gives them to Gemini as context at a quiet moment after its reply has played (anything sent while Gemini is replying would cut the reply off, so the memories inform the person's next turn; ones already in the prompt or given on this connection are skipped); every 20 user turns or 3 quiet minutes, and when the session ends, Gemini Flash summarises the conversation since the last save and embeds it back into ChromaDB, so a crash or power cut loses only that part
- **Voice biomarker analysis** — on session end, `voice_analyzer.py` analyses the recorded audio for vocal quality, prosody, lexical density, and syntactic complexity to track health trends over time
- **Session audio recording** — full session audio saved for voice analysis

### Web App (`baymax_app.py`)

- Mobile-accessible dashboard served on port 5000. Every page needs a login, and the robot's data needs an account that has paired this robot
- Live conversation transcript feed
- Fall event log with timestamps
- Voice analysis dashboard — tracks metrics across sessions, shows progress toward baseline (5 sessions required)
- Email notifications on falls and voice-biomarker alerts via Gmail SMTP, sent to the registered email of each account paired to this robot

## Setup

Experimental public information tools (personalized headlines, weather, air quality,
currency reference rates, holidays, earthquakes, encyclopedia and book search) are
available as an opt-in toolkit in robot v10 and the included #7 desktop v8 tooling.
See [tools, enabling, data flows and review checks](features/toolkits/EXPERIMENTAL.md).
They are disabled by default; desktop Setup provides group checkboxes and the
live monitor displays tool activity names.

1. **Create and activate virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

2. **Install dependencies** from the lock file (exact versions, CPU-only PyTorch):
   ```bash
   pip install -r requirements.lock
   ```
   `requirements.txt` is the older, loose list. Regenerate the lock from a working venv with `python deploy/make_lock.py > requirements.lock`.

3. **Configure environment variables** — copy `.env.example` to `.env` and fill in:
   - `GOOGLE_API_KEY` — Gemini API key
   - `BAYMAX_EMAIL_FROM` — Gmail address alerts are sent from
   - `BAYMAX_EMAIL_PASSWORD` — Gmail app password
   - `BAYMAX_CLOUD_URL`, `BAYMAX_DEVICE_KEY` — optional, for the tadashirobotics.com uplink (`baymax_cloud.py`)

   (`.env.example` is out of date. `BAYMAX_EMAIL_TO` is no longer used: alerts go to the paired accounts.)

   Per-robot settings (serial, speaker, camera, Wi-Fi radio, fall detection on/off) live in `/etc/ember/device.toml`; see `deploy/device.example.toml`. Without it, the defaults match the original LattePanda.

   To set up a **new unit** from the published Docker image instead, see `deploy/provision/README.md`.

4. **Download the pose landmarker model** (first run downloads automatically):
   The MediaPipe pose model (`pose_landmarker_full.task`) is downloaded on first run if not present. It is not tracked in git due to its size (~9 MB).

5. **ONNX embedding model** — the `all-MiniLM-L6-v2-onnx/` directory (`model.onnx` and `tokenizer.json`) must be present for semantic memory. Where memory's packages are installed (ChromaDB, ONNX Runtime and `tokenizers`; the Docker image and the Orange Pi profile), Ember downloads it in the background on start if missing: about 90 MB from Hugging Face, pinned to one revision and checked by SHA-256. `python semantic_embedder.py --download` fetches it beforehand; `convert_model.py` builds the same model where optimum and torch are installed (the Docker image does this while it builds).

## Running

Baymax starts automatically on boot via `startup.sh` (configured as a systemd service). To run manually:

```bash
source venv/bin/activate
python3 realtime_gemini_10.py  # AI core
python3 baymax_app.py          # Web dashboard (separate terminal)
```

Press `Ctrl+C` to end a session. On exit, Baymax will:
1. Summarise the conversation since the last memory save and embed memories into ChromaDB (within 10 s of the stop under the MVP service, `BAYMAX_MEMORY_STOP_SECONDS`)
2. Run voice biomarker analysis on the session audio
3. Restart automatically (when launched via `startup.sh`)

## Data Files (runtime, not tracked in git)

| File | Contents |
|---|---|
| `chroma_store/` | ChromaDB semantic memory database |
| `voice_analysis_results.json` | Per-session voice biomarker data |
| `transcript_log.json` | Rolling conversation transcript |
| `fall_log.json` | Fall detection event log |
| `day_utterance/` | Segmented audio clips for voice analysis |

## Desktop development tools (review branch)

See [PC_SETUP.md](PC_SETUP.md) for the Windows-tested Gemini setup, device tests, live conversation monitor, clickable launchers and location/weather toolkit. See [../AGENTS.md](../AGENTS.md) for coding-assistant guidance. This does not change the deployed startup script or version 9.
