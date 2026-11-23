"""Timeline scenarios: cues on one rig clock.

A step scenario (`steps`) runs one thing after another. A timeline scenario puts every
stimulus on one clock instead, so cues can overlap and their timing is known:

    {"name": "noisy-room", "duration_s": 60,
     "timeline": [
       {"at": 0,   "noise": "tv", "seconds": 40, "level_db": -6},
       {"at": 5,   "id": "q", "say": "What is the best month to plant tulips?"},
       {"id": "stop", "after": {"state": "speaking", "since": "q", "ms": 800}, "say": "Wait, stop."},
       {"at": 20,  "show_video": "fall-forward-01.mp4", "marks": {"fall": 3.2}},
       {"at": 30,  "light": 10},
       {"at": 40,  "fault": "network_drop", "seconds": 30}],
     "end": {"state": "listening", "settle_s": 3},
     "checks":  [{"from": "q", "within_s": 30, "expect": {"heard": "tulip"}}],
     "measure": [{"latency": "q", "max_s": 5}]}

Timing:
- `at`: seconds from the timeline's start.
- `after: {"cue": id, "edge": "start"|"end", "ms": N}`: relative to another cue. When
  that cue has a fixed time this is resolved before the run (sample-exact for audio).
- `after: {"state"|"log"|"event": ..., "since": id, "ms": N, "timeout_s": 30}`: N ms after
  the runtime reports a state (or a log/event line) at or after cue `since` started,
  e.g. barge-in "speak 800 ms after Ember starts talking". The anchor comes from the
  runtime's status file (about 0.2-0.4 s late); the reference microphone measures what
  actually happened (`measure: barge_offset`).

Audio cues with fixed times are mixed into one track before the run and played as one
buffer, so their spacing is sample-exact. A relative audio cue is mixed into the rest
of the track when it fires, and playback continues from the current position.

Everything that varies (noise, gaps in the TV babble, the screen-check code) comes from
the trial's seed, and the report records a SHA-256 of what was played.
"""
import hashlib
import re

import numpy as np

from evals.robot import audio

ACTIONS = ("say", "play", "noise", "show", "show_image", "show_video", "clear_display", "light", "fault",
           "power", "mark")
AUDIO_ACTIONS = ("say", "play", "noise")
CUE_KEYS = set(ACTIONS) | {"at", "after", "id", "level_db", "distance_m", "voice", "seconds", "marks", "sha256",
                           "about"}
AFTER_KEYS = {"cue", "edge", "ms", "state", "log", "event", "since", "timeout_s"}
SCENARIO_KEYS = {"name", "about", "timeline", "duration_s", "end", "checks", "measure", "requires", "gate", "modes",
                 "timeout_s", "fresh_runtime", "late_tolerance_ms", "boot_ready_within_s"}
MEASURE_KINDS = ("latency", "speech_after", "barge_offset")
LATE_TOLERANCE_MS = 150.0     # A display, light or fault cue later than this is a rig fault.
VIDEO_TOLERANCE_MS = 1000.0   # Loading a clip takes longer; measurements use the confirmed start anyway.
ACK_TIMEOUT_S = 3.0           # The display must confirm a card or video within this.
LEAD_S = 0.05                 # A relative audio cue starts this far after it fires (restart headroom).
TAIL_S = 3.0
FINE_POLL_S = 0.05


class TimelineError(ValueError):
    pass


# ── Validation ───────────────────────────────────────────────────────────────
def validate(scenario, check_expect=None):
    """Raise TimelineError for a malformed timeline scenario. `check_expect(name, expect)`
    validates expectation dicts (checks.py passes its own)."""
    name = scenario.get("name")
    unknown = set(scenario) - SCENARIO_KEYS
    if unknown:
        raise TimelineError(f"{name}: unknown timeline scenario keys {sorted(unknown)}")
    cues = scenario.get("timeline") or []
    if not cues:
        raise TimelineError(f"{name}: a timeline needs cues")
    if not float(scenario.get("duration_s", 0)) > 0:
        raise TimelineError(f"{name}: a timeline needs duration_s (its time limit)")
    ids = set()
    for index, cue in enumerate(cues):
        where = f"{name} cue {index}"
        unknown = set(cue) - CUE_KEYS
        if unknown:
            raise TimelineError(f"{where}: unknown keys {sorted(unknown)}")
        actions = [a for a in ACTIONS if a in cue]
        if len(actions) != 1:
            raise TimelineError(f"{where}: needs exactly one of {ACTIONS}")
        if ("at" in cue) == ("after" in cue):
            raise TimelineError(f"{where}: needs exactly one of 'at' or 'after'")
        if "at" in cue and float(cue["at"]) < 0:
            raise TimelineError(f"{where}: 'at' must be >= 0")
        if "after" in cue:
            after = cue["after"]
            if set(after) - AFTER_KEYS:
                raise TimelineError(f"{where}: unknown 'after' keys {sorted(set(after) - AFTER_KEYS)}")
            anchors = [k for k in ("cue", "state", "log", "event") if k in after]
            if len(anchors) != 1:
                raise TimelineError(f"{where}: 'after' needs one of cue, state, log or event")
            for ref in (after.get("cue"), after.get("since")):
                if ref is not None and ref not in ids:
                    raise TimelineError(f"{where}: refers to cue {ref!r}, which must be defined earlier")
            if after.get("edge", "start") not in ("start", "end"):
                raise TimelineError(f"{where}: edge must be start or end")
            for key in ("log", "event"):
                if key in after:
                    re.compile(after[key])
        if cue.get("id"):
            if cue["id"] in ids:
                raise TimelineError(f"{where}: duplicate id {cue['id']!r}")
            ids.add(cue["id"])
        if "noise" in cue:
            if cue["noise"] not in audio.NOISES:
                raise TimelineError(f"{where}: unknown noise {cue['noise']!r}")
            if not cue.get("seconds"):
                raise TimelineError(f"{where}: a noise cue needs seconds")
        if "fault" in cue and not cue.get("seconds"):
            raise TimelineError(f"{where}: a fault cue needs seconds (it is always restored)")
        if "light" in cue and not 0 <= float(cue["light"]) <= 100:
            raise TimelineError(f"{where}: light is a percentage 0-100")
        if "power" in cue and cue["power"] != "cycle":
            raise TimelineError(f"{where}: power supports only 'cycle'")
        if cue.get("sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", cue["sha256"]):
            raise TimelineError(f"{where}: sha256 must be 64 lowercase hex digits")
    for check in scenario.get("checks", []):
        if set(check) - {"from", "edge", "within_s", "expect"}:
            raise TimelineError(f"{name}: unknown check keys in {check}")
        if check.get("from", "start") != "start" and check["from"] not in ids:
            raise TimelineError(f"{name}: check from unknown cue {check['from']!r}")
        if not float(check.get("within_s", 0)) > 0:
            raise TimelineError(f"{name}: a check needs within_s")
        if check_expect:
            check_expect(name, check.get("expect", {}))
    for spec in scenario.get("measure", []):
        kinds = [k for k in MEASURE_KINDS if k in spec]
        if len(kinds) != 1:
            raise TimelineError(f"{name}: a measure needs one of {MEASURE_KINDS}")
        if spec[kinds[0]] not in ids:
            raise TimelineError(f"{name}: measure of unknown cue {spec[kinds[0]]!r}")
        if set(spec) - set(MEASURE_KINDS) - {"max_s", "min_s", "offset_s", "mark"}:
            raise TimelineError(f"{name}: unknown measure keys in {spec}")
    end = scenario.get("end")
    if end is not None and set(end) - {"state", "settle_s"}:
        raise TimelineError(f"{name}: unknown end keys in {end}")


def fixtures_of(scenario):
    """{fixture name: pinned sha256 or None} for the files a scenario plays or shows."""
    found = {}
    for cue in scenario.get("timeline", []):
        for key in ("play", "show_video", "show_image"):
            if key in cue:
                found[cue[key]] = cue.get("sha256") or found.get(cue[key])
    return found  # Step scenarios' show_image files are committed beside scenarios.json.


def describe(cue):
    action = next(a for a in ACTIONS if a in cue)
    when = f"t={float(cue['at']):g}s" if "at" in cue else "after " + ", ".join(
        f"{k}={v}" for k, v in cue["after"].items())
    detail = cue[action]
    if action == "noise":
        detail = f"{cue['noise']} {cue['seconds']}s"
    elif action == "fault":
        detail = f"{cue['fault']} for {cue['seconds']}s"
    return f"{when}: {action} {detail}"


def trial_seed(seed, name, repeat):
    """A per-trial seed that depends only on the run seed, the scenario and the repeat number."""
    return int(hashlib.sha256(f"{seed}:{name}:{repeat}".encode()).hexdigest()[:8], 16)


# ── Audio track ──────────────────────────────────────────────────────────────
class AudioTrack:
    """Float samples from t=0 at audio.RATE; cues are added at sample positions."""

    def __init__(self):
        self.samples = np.zeros(0, dtype=np.float64)

    @property
    def seconds(self):
        return len(self.samples) / audio.RATE

    def add(self, samples, at_s):
        start = int(round(at_s * audio.RATE))
        samples = np.asarray(samples, dtype=np.float64)
        end = start + len(samples)
        if end > len(self.samples):
            self.samples = np.concatenate((self.samples, np.zeros(end - len(self.samples))))
        self.samples[start:end] += samples
        return start / audio.RATE

    def pcm(self, from_s=0.0):
        """int16 from `from_s` on, and whether the mix clipped."""
        part = self.samples[int(round(from_s * audio.RATE)):]
        clipped = bool(np.any(np.abs(part) > 32767))
        return np.clip(part, -32768, 32767).astype(np.int16), clipped


def digest(samples):
    return hashlib.sha256(np.asarray(samples, dtype=np.int16).tobytes()).hexdigest()


# ── Player ───────────────────────────────────────────────────────────────────
class Player:
    """Runs one timeline on the rig. `rig` provides the clock, devices and observer."""

    def __init__(self, rig, scenario, seed=0):
        self.rig, self.scenario, self.seed = rig, scenario, seed
        self.cues = [dict(cue, _index=i, _id=cue.get("id") or f"cue{i}") for i, cue in enumerate(scenario["timeline"])]
        self.by_id = {cue["_id"]: cue for cue in self.cues}
        self.track, self.log, self.played, self.rig_faults, self.failures = AudioTrack(), [], [], [], []
        self.tolerance_ms = float(scenario.get("late_tolerance_ms", LATE_TOLERANCE_MS))
        self.audio_start, self.t0 = None, None
        self.active_faults = {}        # name -> restore time (timeline seconds)
        self.acks = {}                 # cue id -> (version, event, deadline)

    # ── Before the run ───────────────────────────────────────────────────────
    def render(self, cue):
        """Leveled int16 samples of an audio cue (no padding), deterministic for the seed."""
        action = next(a for a in AUDIO_ACTIONS if a in cue)
        rng_seed = (self.seed + cue["_index"] * 7919) % (2 ** 32)
        if action == "say":
            raw = self.rig.speak(cue["say"], cue.get("voice", self.rig.voice))
            label = cue["say"]
        elif action == "noise":
            raw = audio.noise(cue["noise"], float(cue["seconds"]), self.rig.speak, seed=rng_seed)
            label = f"{cue['noise']} noise {cue['seconds']}s"
        else:
            raw = self.rig.fixture_audio(cue["play"])
            label = cue["play"]
        samples, clipped = audio.at_level(raw, self.rig.level(cue), self.rig.gain_db)
        if clipped:
            self.rig_faults.append(f"cue {cue['_id']} clipped at its level")
        return samples, label

    def static_time(self, cue, resolved):
        """Timeline seconds of a cue known before the run, else None (state/log anchored)."""
        if "at" in cue:
            return float(cue["at"])
        after = cue["after"]
        if "cue" not in after:
            return None
        anchor = self.by_id[after["cue"]]
        start = resolved.get(anchor["_id"])
        if start is None:
            return None
        offset = anchor.get("_seconds", 0.0) if after.get("edge", "start") == "end" else 0.0
        return start + offset + float(after.get("ms", 0)) / 1000.0

    def prepare(self):
        resolved = {}
        for cue in self.cues:
            if any(a in cue for a in AUDIO_ACTIONS):
                cue["_samples"], cue["_label"] = self.render(cue)
                cue["_seconds"] = len(cue["_samples"]) / audio.RATE
            elif "fault" in cue:
                cue["_seconds"] = float(cue["seconds"])
            else:
                cue["_seconds"] = 0.0
            cue["_time"] = self.static_time(cue, resolved)
            if cue["_time"] is not None:
                resolved[cue["_id"]] = cue["_time"]
                if "_samples" in cue:
                    self.track.add(cue["_samples"], cue["_time"])
        mix, clipped = self.track.pcm()
        if clipped:
            self.rig_faults.append("the mixed audio track clipped")
        sounds = sorted((c["_id"], digest(c["_samples"])) for c in self.cues if "_samples" in c)
        self.stimulus_sha256 = hashlib.sha256(
            (digest(mix) + "".join(f"{i}:{d}" for i, d in sounds)).encode()).hexdigest()
        return self.stimulus_sha256

    # ── During the run ───────────────────────────────────────────────────────
    def now(self):
        return self.rig.clock.time() - self.t0

    def start_audio(self, from_s, parts):
        pcm, _ = self.track.pcm(from_s)
        if not len(pcm):
            return
        self.rig.speaker.stop()
        self.rig.speaker.start(pcm, self.scenario["name"], parts=parts)
        self.audio_start = self.rig.clock.time() - from_s  # rig time of track t=0

    def parts(self, cues, from_s):
        return [(c["_time"] - from_s, c["_label"], c["_seconds"]) for c in cues if c["_time"] >= from_s - 1e-9]

    def record(self, cue, fired, actual=None, note=None):
        scheduled = cue.get("_due", cue.get("_time"))
        entry = {"id": cue["_id"], "cue": describe(cue), "scheduled_s": None if scheduled is None else round(scheduled, 3),
                 "fired_s": round(fired, 3), "actual_s": None if actual is None else round(actual, 3)}
        if scheduled is not None and actual is not None:
            entry["late_ms"] = round((actual - scheduled) * 1000, 1)
        if note:
            entry["note"] = note
        cue["_fired"], cue["_actual"] = fired, actual if actual is not None else fired
        self.log.append(entry)
        return entry

    def anchor_time(self, cue):
        """Rig time the runtime reported the anchor, or None."""
        after = cue["after"]
        since_cue = self.by_id.get(after.get("since")) if after.get("since") else None
        if since_cue is not None and "_actual" not in since_cue:
            return None
        since = self.t0 + (since_cue["_actual"] if since_cue is not None else 0.0)
        observer = self.rig.observer
        if "state" in after:
            return observer.window(since).first(after["state"])
        if "log" in after:
            hits = [when for when, line in observer.log if when >= since and re.search(after["log"], line)]
            return min(hits) if hits else None
        hits = [e["time"] for e in observer.window(since).events()
                if re.search(after["event"], f"{e.get('state')}: {e.get('message', '')}")]
        return min(hits) if hits else None

    def fire(self, cue, now):
        rig = self.rig
        if "_samples" in cue:  # Relative audio: mix into the rest of the track and continue from here.
            if self.audio_start is None:
                self.audio_start = rig.clock.time()
            position = max(rig.clock.time() - self.audio_start, 0.0)
            at = position + LEAD_S
            self.track.add(cue["_samples"], at)
            self.start_audio(position, [(LEAD_S, cue["_label"], cue["_seconds"])])
            actual = (self.audio_start - self.t0) + at
            cue["_time"] = actual
            self.played.append({"id": cue["_id"], "start": self.t0 + actual, "samples": cue["_samples"]})
            return self.record(cue, now, actual)
        if "show_video" in cue:
            version = rig.display.play_video(rig.fixture_path(cue["show_video"]), cue["show_video"])
            self.acks[cue["_id"]] = (version, "playing", now + ACK_TIMEOUT_S)
            return self.record(cue, now, None, "waiting for the display to confirm playback")
        if "show" in cue or "show_image" in cue:
            from evals import live_eval
            jpeg = live_eval.card(cue["show"]) if "show" in cue else rig.fixture_path(cue["show_image"]).read_bytes()
            version = rig.display.show(jpeg, " / ".join(cue["show"]) if "show" in cue else cue["show_image"])
            if hasattr(rig.display, "ack"):
                self.acks[cue["_id"]] = (version, "shown", now + ACK_TIMEOUT_S)
            return self.record(cue, now, None if hasattr(rig.display, "ack") else now)
        if "clear_display" in cue:
            rig.display.clear()
        elif "light" in cue:
            rig.light.set(float(cue["light"]))
        elif "fault" in cue:
            rig.faults[cue["fault"]].start()
            self.active_faults[cue["fault"]] = now + float(cue["seconds"])
        elif "power" in cue:
            boot = rig.power_cycle()
            entry = self.record(cue, now, now)
            entry["boot_ready_s"] = boot
            return entry
        return self.record(cue, now, self.now())

    def check_acks(self, now):
        for cue_id, (version, event, deadline) in list(self.acks.items()):
            at = self.rig.display.ack(version, event)
            cue = self.by_id[cue_id]
            entry = next(e for e in self.log if e["id"] == cue_id)
            if at is not None:
                actual = at - self.t0
                entry["actual_s"] = round(actual, 3)
                entry.pop("note", None)
                if cue.get("_time") is not None:
                    entry["late_ms"] = round((actual - cue["_time"]) * 1000, 1)
                    tolerance = max(self.tolerance_ms, VIDEO_TOLERANCE_MS) if event == "playing" else self.tolerance_ms
                    if entry["late_ms"] > tolerance:
                        self.rig_faults.append(f"cue {cue_id}: display confirmed {event} {entry['late_ms']:.0f} ms late")
                cue["_actual"] = actual
                del self.acks[cue_id]
            elif now > deadline:
                self.rig_faults.append(f"display did not confirm {event} of cue {cue_id} within {ACK_TIMEOUT_S:.0f}s")
                entry["note"] = "no confirmation from the display"
                del self.acks[cue_id]

    def restore_faults(self, now=None, everything=False):
        for name, until in list(self.active_faults.items()):
            if everything or (now is not None and now >= until):
                try:
                    self.rig.faults[name].stop()
                except Exception as error:  # Restoring is the most important thing; keep going.
                    self.rig_faults.append(f"could not restore fault {name}: {type(error).__name__}: {error}")
                self.log.append({"id": f"restore:{name}", "cue": f"restore {name}", "fired_s": round(self.now(), 3)})
                del self.active_faults[name]

    def finished(self, now):
        if any("_fired" not in c and "_failed" not in c for c in self.cues) or self.acks or self.active_faults:
            return False
        audio_end = 0.0 if self.audio_start is None else (self.audio_start - self.t0) + self.track.seconds
        last = max([c["_actual"] for c in self.cues if "_actual" in c] + [audio_end])
        end = self.scenario.get("end")
        if not end:
            return now >= max(last, audio_end) + TAIL_S
        polled = self.rig.observer.polled
        settled = self.rig.clock.time() - self.rig.observer.last_change >= float(end.get("settle_s", 2.0))
        return now >= audio_end and bool(polled) and polled[-1][1] == end.get("state", "listening") and settled \
            and now >= last + float(end.get("settle_s", 2.0))

    def run(self):
        rig = self.rig
        self.t0 = rig.clock.time()
        static_audio = [c for c in self.cues if "_samples" in c and c["_time"] is not None]
        if len(self.track.samples):
            self.start_audio(0.0, self.parts(static_audio, 0.0))
        for cue in static_audio:
            self.played.append({"id": cue["_id"], "start": self.audio_start + cue["_time"], "samples": cue["_samples"]})
        limit = float(self.scenario["duration_s"])
        try:
            while True:
                rig.tick()
                now = self.now()
                for cue in self.cues:
                    if "_fired" in cue or "_failed" in cue:
                        continue
                    if "_samples" in cue and cue["_time"] is not None:  # Already in the playing track.
                        if now >= cue["_time"]:
                            self.record(cue, now, (self.audio_start - self.t0) + cue["_time"])
                        continue
                    due = self.due(cue, now)
                    if due is None or now < due:
                        continue
                    cue["_due"] = due
                    if "_samples" not in cue:
                        cue["_time"] = due
                    entry = self.fire(cue, now)
                    if "_anchor" in cue:
                        entry["anchor_s"] = round(cue["_anchor"], 3)
                    late = entry.get("late_ms")
                    if late is not None and late > self.tolerance_ms:
                        self.rig_faults.append(f"cue {cue['_id']} fired {late:.0f} ms late")
                self.check_acks(now)
                self.restore_faults(now)
                if self.finished(now):
                    break
                if now >= limit:
                    pending = [c["_id"] for c in self.cues if "_fired" not in c and "_failed" not in c]
                    if pending:
                        self.failures.append(f"timeline limit {limit:g}s reached before cues {pending}")
                    break
                rig.clock.sleep(max(0.005, min(FINE_POLL_S, self.next_due(now) - now)))
        finally:
            self.restore_faults(everything=True)
            try:
                rig.speaker.stop()
            except Exception:
                pass
        return self.log

    def due(self, cue, now):
        """Timeline seconds a not-yet-fired cue is due, or None while its anchor is unknown."""
        if cue.get("_time") is not None:
            return cue["_time"]
        after = cue["after"]
        if "cue" in after:  # Relative to a cue whose time was only known during the run.
            ref = self.by_id[after["cue"]]
            if "_actual" not in ref:
                return None
            edge = ref["_seconds"] if after.get("edge", "start") == "end" else 0.0
            return ref["_actual"] + edge + float(after.get("ms", 0)) / 1000.0
        anchor = self.anchor_time(cue)
        if anchor is not None:
            cue["_anchor"] = anchor - self.t0
            return cue["_anchor"] + float(after.get("ms", 0)) / 1000.0
        since = self.by_id.get(after.get("since"))
        base = since.get("_actual") if since else 0.0
        if base is not None and now > base + float(after.get("timeout_s", 30)):
            cue["_failed"] = True
            self.failures.append(f"cue {cue['_id']}: anchor {after} did not happen")
            self.log.append({"id": cue["_id"], "cue": describe(cue), "fired_s": None, "note": "anchor did not happen"})
        return None

    def next_due(self, now):
        times = [c["_time"] for c in self.cues if "_fired" not in c and c.get("_time") is not None]
        return min([t for t in times if t > now] + [now + FINE_POLL_S])

    def cue_rig_time(self, cue_id, edge="start"):
        cue = self.by_id[cue_id]
        if "_actual" not in cue:
            return None
        return self.t0 + cue["_actual"] + (cue["_seconds"] if edge == "end" else 0.0)
