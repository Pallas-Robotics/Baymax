"""Local devUI: configure key and PC devices, without starting Gemini."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import urllib.request

import sounddevice as sd
from dev_settings import public_configuration, save_configuration
from pc_hardware import open_camera
from runtime_monitor import read_status

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
FALL_DEMO = ROOT / ".runtime-fall-demo.json"   # same file the runtime polls (fall_detection.DEMO_PATH)
PAGES = {"/": "static/devUI.html", "/monitor": "static/emberdevUI.html", "/emberdevUI": "static/emberdevUI.html",
         "/fall-demo": "static/falldemo.html"}


def offline_replay(name):
    """Ember is not running: compute the synthetic replay here so the page can animate it. Nobody is alerted."""
    from features.toolkits import fall_poses
    from features.toolkits.fall_logic import replay, points, V10_FPS
    scenario = fall_poses.scenario(name, V10_FPS)
    steps = []
    for (moment, landmarks), result in zip(scenario.frames, replay(scenario.frames)):
        angle, countdown = result["trunk_angle"], result["countdown_s"]
        steps.append({"t": moment, "points": points(landmarks), "posture": result["posture"],
                      "trunk_angle": None if angle is None else round(angle, 1),
                      "fall_pending": result["fall_pending"], "fall_detected": result["fall_detected"],
                      "countdown_s": None if countdown is None else round(countdown, 2)})
    return {"scenario": name, "description": scenario.description, "steps": steps}


def fall_demo(body):
    from features.toolkits import fall_poses
    from features.toolkits.fall_detection import WOULD_FIRE_CAMERA, WOULD_FIRE_REPLAY
    action = body.get("action")
    enabled, speak = body.get("enabled", True), body.get("speak", True)
    if action not in ("set", "replay") or not isinstance(enabled, bool) or not isinstance(speak, bool):
        raise ValueError("Invalid fall demo request.")
    scenario = body.get("scenario", "fast_fall")
    if action == "replay" and scenario not in fall_poses.DEMO_SCENARIOS:
        raise ValueError("Unknown synthetic scenario.")
    would_fire = {"replay": list(WOULD_FIRE_REPLAY), "camera": list(WOULD_FIRE_CAMERA)}
    status = read_status()
    running = status.get("state") not in ("offline", "stopped") and isinstance(status.get("pid"), int)
    if not running:
        result = {"runtime": False, "would_fire": would_fire}
        if action == "replay":
            result["timeline"] = offline_replay(scenario)
        return result
    request = {"pid": status["pid"], "enabled": enabled or action == "replay", "speak": speak,
               "updated_at": time.time()}
    if action == "replay":
        request["replay"] = {"id": uuid.uuid4().hex, "scenario": scenario, "requested_at": time.time()}
    temporary = FALL_DEMO.with_suffix(".tmp")
    temporary.write_text(json.dumps(request), encoding="utf-8")
    os.replace(temporary, FALL_DEMO)
    return {"runtime": True, "would_fire": would_fire, "enabled": request["enabled"], "speak": speak}


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        # Windows otherwise permits multiple development servers on one port.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def audio_catalog():
    # PortAudio caches devices at initialization. A fresh process sees hot-plugged
    # hardware without resetting audio streams in another running application.
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--audio-catalog"],
                            capture_output=True, text=True, check=True, timeout=20)
    return json.loads(result.stdout)


def _audio_catalog_snapshot():
    hosts = sd.query_hostapis()
    devices = sd.query_devices()
    result = {"microphones": [], "speakers": []}
    for index, device in enumerate(devices):
        # WASAPI exposes the full Windows endpoint name without duplicate driver entries.
        if sys.platform == "win32" and hosts[device["hostapi"]]["name"] != "Windows WASAPI":
            continue
        entry = {"index": index, "name": device["name"], "hostapi": hosts[device["hostapi"]]["name"]}
        if device["max_input_channels"] > 0:
            result["microphones"].append(entry)
        if device["max_output_channels"] > 0:
            result["speakers"].append(entry)
    return result


def camera_catalog():
    if sys.platform == "darwin":
        from macos_devices import camera_devices
        return camera_devices()  # Names/IDs only; Test camera requests actual capture.
    os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
    import cv2
    cameras = []
    names = []
    if sys.platform == "win32":
        # COM enumeration must be initialized in this worker thread.
        import comtypes
        from pygrabber.dshow_graph import FilterGraph
        comtypes.CoInitialize()
        try:
            names = FilterGraph().get_input_devices()
        finally:
            comtypes.CoUninitialize()
    for index in range(len(names) if sys.platform == "win32" else 10):
        camera = open_camera(cv2, index)
        try:
            if camera.isOpened():
                ready, frame = camera.read()
                if ready and frame is not None:
                    height, width = frame.shape[:2]
                    cameras.append({"index": index, "name": names[index] if names else f"Camera {index}",
                                    "resolution": f"{width} × {height}"})
        finally:
            camera.release()
    return cameras


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, code, data, content_type="application/json"):
        body = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}",
                                             f"localhost:{self.server.server_port}")

    def do_GET(self):
        if not self.valid_host():
            self.respond(403, {"error": "Local host required."})
        elif self.path in PAGES:
            page = (ROOT / PAGES[self.path]).read_text(encoding="utf-8").replace("__TOKEN__", self.server.token)
            self.respond(200, page.encode(), "text/html; charset=utf-8")
        else:
            self.respond(404, {"error": "Not found."})

    def do_POST(self):
        origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if (not self.valid_host() or self.headers.get("Origin") not in origins or
                not secrets.compare_digest(self.headers.get("X-devUI-Token", ""), self.server.token)):
            self.respond(403, {"error": "Refresh the local devUI page and try again."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16384:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(length))
            if self.path == "/api/status":
                self.respond(200, public_configuration(ENV))
            elif self.path == "/api/runtime":
                self.respond(200, read_status())
            elif self.path == "/api/fall-demo":
                self.respond(200, fall_demo(body))
            elif self.path == "/api/devices":
                # Only one hardware scan at a time, including requests from multiple tabs.
                with self.server.settings_lock:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        audio = pool.submit(audio_catalog)
                        camera = pool.submit(camera_catalog)
                        catalog = audio.result()
                        catalog["cameras"] = camera.result()
                    self.server.catalog = catalog
                self.respond(200, catalog)
            elif self.path == "/api/save":
                with self.server.settings_lock:
                    if getattr(self.server, "setup_once", False) and any(
                        not body.get(field) for field in ("microphone", "speaker", "camera")
                    ):
                        raise ValueError("Select explicit microphone, speaker and camera devices (or No camera) for MVP.")
                    if self.server.catalog is None:
                        raise ValueError("Wait for device discovery before saving.")
                    current_audio = audio_catalog()
                    cameras = self.server.catalog["cameras"]
                    if sys.platform == "darwin" and body.get("camera") not in ("", "none"):
                        selected = next((c for c in cameras if str(c["index"]) == body.get("camera")), None)
                        fresh = camera_catalog()
                        matches = [c for c in fresh if selected and c["uid"] == selected.get("uid")]
                        if len(matches) != 1:
                            raise ValueError("Camera changed or disconnected. Refresh devices and choose again.")
                        body["camera"] = str(matches[0]["index"])
                        cameras = fresh
                    current_audio["cameras"] = cameras
                    result = save_configuration(ENV, body, current_audio)
                    if getattr(self.server, "setup_once", False):
                        result["mvp_starting"] = result["key_configured"]
                    self.respond(200, result)
                    if getattr(self.server, "setup_once", False) and result["key_configured"]:
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif self.path == "/api/test":
                kind, device = body.get("kind"), body.get("device", "")
                if kind not in ("microphone", "speaker", "camera") or not isinstance(device, str):
                    raise ValueError("Invalid test request.")
                with self.server.settings_lock:
                    command = [sys.executable, str(ROOT / "device_test.py"), kind, device]
                    if kind == "camera" and device not in ("", "none") and sys.platform == "darwin":
                        selected = next((c for c in (self.server.catalog or {}).get("cameras", [])
                                         if str(c["index"]) == device), None)
                        if not selected or not selected.get("uid"):
                            raise ValueError("Refresh devices before testing this camera.")
                        command.append(selected["uid"])
                    result = subprocess.run(command,
                                            capture_output=True, text=True, timeout=20)
                    data = json.loads(result.stdout)
                self.respond(400 if result.returncode else 200, data)
            else:
                self.respond(404, {"error": "Not found."})
        except (ValueError, RuntimeError, json.JSONDecodeError) as error:
            self.respond(400, {"error": str(error)})
        except Exception:
            self.respond(500, {"error": "Could not read devices or save settings. Close apps using the camera, check file permissions, and refresh."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--setup-once", action="store_true", help="Close setup after saving; the MVP launcher then starts Ember.")
    args = parser.parse_args()
    try:
        server = LocalServer(("127.0.0.1", args.port), Handler)
    except OSError:
        url = f"http://127.0.0.1:{args.port}"
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                existing = response.read(65536)
            if b"<title>Ember devUI</title>" in existing:
                if not args.no_browser:
                    webbrowser.open(url + ("/monitor" if args.monitor else ""))
                parser.exit(0, f"devUI is already running at {url}\n")
        except OSError:
            pass
        parser.exit(1, "devUI's port is busy. Open the existing devUI or use --port 8767.\n")
    server.token = secrets.token_urlsafe(32)
    server.setup_once = args.setup_once
    server.catalog = None
    server.settings_lock = threading.Lock()
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"devUI: {url}\nSave settings here, then start/restart Gemini. Ctrl+C closes devUI.", flush=True)
    if not args.no_browser:
        webbrowser.open(url + ("/monitor" if args.monitor else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--audio-catalog"]:
        print(json.dumps(_audio_catalog_snapshot()))
    else:
        main()
