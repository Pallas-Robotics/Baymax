"""Single-threaded expressions, delayed thinking and priority fall-alert eyes."""
from contextlib import contextmanager
import os
from pathlib import Path
import random
import sys
import threading
import time

from .driver import open_leds
from .patterns import EXPRESSIONS

LATTEPANDA_PORT = "/dev/serial/by-id/usb-Unknown_LattePanda_Leonardo-if00"
ORANGE_PI_SPI = "/dev/spidev3.1"   # Orange Pi 4 Pro SPI3 CS1: DIN pin 19, CLK 23, CS 26
# The LattePanda has no spidev3.1, and the Orange Pi image enables it (spi3-cs0-cs1-spidev).
PORT = ORANGE_PI_SPI if os.path.exists(ORANGE_PI_SPI) else LATTEPANDA_PORT
OPEN = (0x3C, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x3C)
BLINK = (0, 0, 0, 0, 0x3C, 0, 0, 0)
BLANK = (0,) * 8
DOTS = tuple((0, 0, 0, 0, value, value, 0, 0) for value in (0xC0, 0x18, 0x03))
THINKING = tuple((dot, BLANK) for dot in DOTS) + tuple((BLANK, dot) for dot in DOTS)
BRIGHTNESS = 2   # of 15; one step dimmer on the Orange Pi build
SLEEP_BRIGHTNESS = 1  # Sleep eyes are dimmer.
# Anything that means a conversation is happening resets the sleep timer; the
# last of Ember's audio leaving the speaker too, so the quiet time counts from
# when Ember actually stops talking, not from Gemini's last packet.
CONVERSATION_EVENTS = {'user_speech', 'thinking', 'speaking', 'calling_tool', 'reply_complete',
                       'fall_alert'}
# Consecutive MVP starts that never reached Gemini; X eyes ("unplug and replug") after 3.
FAILURES = Path(__file__).resolve().parents[2] / ".eyes-failures"
FAILED_STARTS_FOR_X = 3


def failure_face(connected, path=FAILURES):
    """Face left on the matrices after a failed run: confused while systemd retries, then X."""
    try:
        count = int(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        count = 0
    count = 1 if connected else count + 1
    try:
        path.write_text(str(count), encoding="utf-8")
    except OSError:
        pass
    return 'dead' if count >= FAILED_STARTS_FOR_X else 'confused'


SENSOR_STALL = {'microphone': 5.0, 'speaker': 5.0, 'camera': 10.0}
SENSOR_GRACE = 30.0  # After first connecting, every expected sensor must have reported.


def expected_sensors(env=os.environ):
    expected = []
    if env.get('BAYMAX_AUDIO_ENHANCEMENT', '0') == '1':  # Audio heartbeats come from the cleanup stage.
        expected += ['microphone', 'speaker']
    if env.get('BAYMAX_CAMERA_DEVICE', '').strip() != 'none':
        expected.append('camera')
    return expected


def lost_sensor(seen, expected, now, connected_at, ready_at):
    """First expected sensor that went quiet while connected; silence counts from (re)connect."""
    for name in expected:
        last = seen.get(name)
        if last is None:
            if now - connected_at > SENSOR_GRACE:
                return name
        elif now - max(last, ready_at) > SENSOR_STALL[name]:
            return name
    return None


def clear_failures(path=FAILURES):
    try:
        path.unlink()
    except OSError:
        pass


class Eyes:
    def __init__(self, port=PORT, board_factory=None, clock=time.monotonic, booting=False, dead_after=0,
                 sensors=None, sleep_after=0, wait_for_mic=False):
        self.port, self.board_factory, self.clock = port, board_factory, clock
        # Confused spinner while Gemini is not connected: from launch (booting) and
        # while reconnecting. X eyes once unreachable this long (0 disables).
        self.booting = booting
        self.dead_after = dead_after
        self.unready_since = clock() if booting else None
        self.offline_since = None  # Set by NetworkWatch: no route to Gemini.
        # Dimmed sleep eyes after this long without conversation (0 disables).
        self.sleep_after = sleep_after
        self.disengaged = False
        self.last_activity = clock()
        # sensors(now, connected_at, ready_at) -> name of a lost sensor or None.
        self.sensors = sensors
        self.lost = None
        self.connected_at = self.ready_at = None
        self.connected_once = False
        # Ready only once mic audio is actually reaching Gemini ('mic_live' from the
        # audio cleanup), not merely when the session connects; 'mic_stalled' undoes it.
        self.wait_for_mic = wait_for_mic
        self.awaiting_mic = False
        self.exit_failed = False
        self.final_face = None  # Left on the matrices at exit instead of blanking them.
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.waiting_since = None
        self.responding = False
        self.thread = None
        self.expression = 'normal'
        self.expression_until = 0
        self.fall_until = 0
        self.fall_speaking = False
        self.reply_complete = False

    def express(self, name):
        if name not in ('normal', *EXPRESSIONS):
            return False
        if self.stopping.is_set() or (self.thread is not None and not self.thread.is_alive()):
            return False
        with self.lock:
            self.expression = name
            self.expression_until = self.clock() + 30
            self.last_activity = self.clock()
        self.wake.set()
        return True

    def _ready(self):
        # Caller holds self.lock.
        self.awaiting_mic = False
        self.ready_at = self.clock()
        self.last_activity = self.ready_at  # Awake for a while after (re)connecting.
        if self.connected_at is None:
            self.connected_at = self.ready_at
        self.unready_since = None

    def event(self, event):
        """Request state only; no serial writes on the Gemini/audio thread."""
        with self.lock:
            if event in CONVERSATION_EVENTS:
                self.last_activity = self.clock()
            if event == "listening":
                self.booting = False
                self.connected_once = True
                if self.unready_since is not None or self.ready_at is None:
                    if self.wait_for_mic:
                        self.awaiting_mic = True  # Keep the spinner until the mic is live.
                    else:
                        self._ready()
            elif event == "mic_live":
                if self.awaiting_mic:
                    self._ready()
            elif event == "mic_stalled":
                if self.wait_for_mic and self.connected_once and self.unready_since is None:
                    self.unready_since = self.clock()
                    self.awaiting_mic = True
            elif event in ("connecting", "reconnecting"):
                self.awaiting_mic = False
                if self.unready_since is None:
                    self.unready_since = self.clock()
            elif event == "network_lost" and self.offline_since is None:
                self.offline_since = self.clock()
            elif event == "network_ok":
                self.offline_since = None
            elif event == "disengaged":
                self.disengaged = True
            elif event == "engaged":
                self.disengaged = False
                self.last_activity = self.clock()
            if event == "user_speech":
                if not self.responding:
                    if self.waiting_since is None:
                        self.expression = 'normal'
                    self.waiting_since = self.clock()
            elif event == "thinking":
                if not self.responding and self.waiting_since is None:
                    self.waiting_since = self.clock()
            elif event == "speaking":
                self.responding = True
                self.waiting_since = None
                self.reply_complete = False
                if self.clock() < self.fall_until:
                    self.fall_speaking = True
            elif event == 'fall_alert':
                self.fall_until = self.clock() + 15
                self.fall_speaking = False
                self.waiting_since = None
            elif event == 'reply_complete':
                self.responding = False
                self.waiting_since = None
                self.reply_complete = True
            elif event == 'reply_playback_done':
                if self.reply_complete:
                    # Empty-buffer heartbeats repeat; only the actual reply end is activity.
                    self.last_activity = self.clock()
                    self.expression = 'normal'
                    if self.fall_speaking:
                        self.fall_until = 0
                    self.reply_complete = False
            elif event in ('calling_tool', 'network_lost', 'network_ok', 'mic_live', 'mic_stalled',
                           'disengaged', 'engaged'):
                pass
            else:
                self.responding = False
                self.waiting_since = None
                self.expression = 'normal'
                self.fall_until = 0
                self.reply_complete = False
        self.wake.set()

    def mode(self):
        with self.lock:
            now = self.clock()
            if now < self.fall_until:
                return 'surprised'
            down = [t for t in (self.unready_since, self.offline_since) if t is not None]
            if down:
                timed_out = self.dead_after and now - min(down) > self.dead_after
                return 'dead' if timed_out else 'confused'
            if self.lost_sensor(now):
                return 'dead'
            if self.disengaged:  # "Ember disengage": sleep eyes until "Ember engage".
                return 'sleep'
            if self.sleep_after and self.connected_once and now - self.last_activity > self.sleep_after:
                return 'sleep'
            if self.waiting_since is not None and now - self.waiting_since > 2.0:
                return 'thinking'
            return self.expression if now < self.expression_until else 'normal'

    def lost_sensor(self, now):
        # Called under self.lock; only meaningful while connected.
        lost = None
        if self.sensors is not None and self.connected_at is not None:
            try:
                lost = self.sensors(now, self.connected_at, self.ready_at)
            except Exception:
                lost = None
        if lost != self.lost:
            self.lost = lost
            print(f"[EYES] Lost {lost}; showing X (unplug and replug)." if lost else "[EYES] Sensors reporting again.",
                  flush=True)
        return lost

    def thinking(self):
        with self.lock:
            return self.waiting_since is not None and self.clock() - self.waiting_since > 2.0

    def hold(self, seconds, thinking):
        end = self.clock() + seconds
        expected = 'thinking' if thinking is True else 'normal' if thinking is False else thinking
        while not self.stopping.is_set() and self.mode() == expected:
            remaining = end - self.clock()
            if remaining <= 0:
                return True
            self.wake.wait(min(remaining, 0.02))
            self.wake.clear()
        return False

    def render(self, leds):
        leds.draw_pair(OPEN)
        next_blink = self.clock() + random.uniform(0.7, 1.4)
        frame = 0
        was_thinking = False
        previous = 'normal'
        dimmed = False
        while not self.stopping.is_set():
            mode = self.mode()
            if mode != previous:
                frame = 0
            if (mode == 'sleep') != dimmed:
                dimmed = mode == 'sleep'
                for device in (0, 1):
                    leds.set_intensity(device, SLEEP_BRIGHTNESS if dimmed else BRIGHTNESS)
            if mode == 'thinking':
                leds.draw_pair(*THINKING[frame % len(THINKING)])
                frame += 1
                self.hold(0.75, True)
                was_thinking = True
            elif mode != 'normal':
                frames, seconds = EXPRESSIONS[mode]
                pair = frames[frame % len(frames)]
                leds.draw_pair(*pair)
                frame += 1
                if mode in ('surprised', 'curious', 'love'):
                    if mode != previous:
                        next_blink = self.clock() + random.uniform(0.7, 1.4)
                    if self.clock() >= next_blink:
                        if random.random() < (0.50 if mode == 'surprised' else 0.55):
                            leds.draw_pair(BLINK)
                            self.hold(0.015, mode)
                            if self.mode() == mode:
                                leds.draw_pair(*pair)
                                if random.random() < 0.15 and self.hold(0.055, mode):
                                    leds.draw_pair(BLINK)
                                    self.hold(0.010, mode)
                                    if self.mode() == mode:
                                        leds.draw_pair(*pair)
                        next_blink = self.clock() + random.uniform(0.7, 1.4)
                    self.hold(max(0, next_blink - self.clock()), mode)
                else:
                    self.hold(seconds, mode)
                was_thinking = True
            else:
                if was_thinking:
                    leds.draw_pair(OPEN)
                    frame = 0
                    was_thinking = False
                    next_blink = self.clock() + random.uniform(0.7, 1.4)
                if self.clock() >= next_blink:
                    if random.random() < 0.75:
                        leds.draw_pair(BLINK)
                        self.hold(0.015, False)
                        leds.draw_pair(OPEN)
                        if not self.thinking() and random.random() < 0.15:
                            if self.hold(0.055, False):
                                leds.draw_pair(BLINK)
                                self.hold(0.010, False)
                                leds.draw_pair(OPEN)
                    next_blink = self.clock() + random.uniform(0.7, 1.4)
                self.hold(max(0, next_blink - self.clock()), False)
            previous = mode

    def run(self):
        close = leds = None
        try:
            leds, close = open_leds(self.port, self.board_factory)
            for device in (0, 1):
                leds.set_intensity(device, BRIGHTNESS)
                leds.shutdown(device, False)
            print("[EYES] Ready: expressive eyes; thinking after 2 seconds; fall surprise override.", flush=True)
            self.render(leds)
        except Exception as error:
            print(f"[EYES] Unavailable (conversation continues): {error}", flush=True)
        finally:
            try:
                if leds is not None and self.final_face in EXPRESSIONS:
                    # The MAX7219s keep showing this after the process exits.
                    leds.draw_pair(*EXPRESSIONS[self.final_face][0][0])
                elif leds is not None:
                    for device in (0, 1):
                        leds.shutdown(device, True)
            except Exception:
                pass
            finally:
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass

    def start(self):
        self.thread = threading.Thread(target=self.run, name="ember-eyes", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=3)


def _mic_reports():
    """The audio cleanup reports when mic audio reaches Gemini; without it, connect = ready."""
    try:
        from features.conversational_ability import processing
        return processing.enabled() and os.getenv("BAYMAX_EYES_WAIT_FOR_MIC", "1") != "0"
    except Exception:
        return False


@contextmanager
def eyes_session():
    if os.getenv("BAYMAX_EYES_ENABLED", "1" if sys.platform.startswith("linux") else "0") != "1":
        yield
        return
    import runtime_monitor as monitor
    def seconds(name, default):
        try:
            return float(os.getenv(name, str(default)))
        except ValueError:
            return float(default)
    dead_after = seconds("BAYMAX_EYES_DEAD_AFTER_S", 120)
    sleep_after = seconds("BAYMAX_EYES_SLEEP_AFTER_S", 60)
    sensors = None
    if os.getenv("BAYMAX_EYES_SENSOR_WATCHDOG", "1") != "0":
        # Expected sensors are read at check time: run_pc enables audio cleanup after this.
        def sensors(now, connected_at, ready_at):
            return lost_sensor(dict(monitor.sensors), expected_sensors(), now, connected_at, ready_at)
    eyes = Eyes(port=os.getenv("BAYMAX_EYES_PORT") or PORT, booting=True, dead_after=dead_after, sensors=sensors,
                sleep_after=sleep_after, wait_for_mic=_mic_reports())
    previous = monitor.event_observer
    previous_expression = monitor.eye_expression_handler
    monitor.event_observer = eyes.event
    monitor.eye_expression_handler = eyes.express
    from . import auto
    detach_auto = auto.attach(eyes.express) if auto.enabled() else (lambda: None)
    eyes.start()
    network = None
    if os.getenv("BAYMAX_EYES_NETWORK_WATCH", "1") != "0":
        from .network import NetworkWatch
        def report(event):
            monitor.record('network', 'Network lost: cannot reach Gemini' if event == 'network_lost' else 'Network back')
            eyes.event(event)
        network = NetworkWatch(report)
        network.start()
    failed = False
    try:
        yield eyes
    except KeyboardInterrupt:
        raise  # Deliberate stop (Ctrl+C / SIGTERM): blank eyes as before.
    except SystemExit as error:
        failed = error.code not in (0, None)
        raise
    except BaseException:
        failed = True
        raise
    finally:
        if network is not None:
            network.stop()
        monitor.event_observer = previous
        monitor.eye_expression_handler = previous_expression
        detach_auto()
        if failed or eyes.exit_failed:
            eyes.final_face = failure_face(eyes.connected_once)
        elif eyes.connected_once:
            clear_failures()
        eyes.stop()
