"""Voice recognition in its own process, on the slow cores.

Ember's process only copies each 20 ms mic block into a bounded queue (VoiceFeed.feed,
called from the mic callback: one deque append, never a wait). A feeder thread sends the
queued blocks to the child every BATCH_S in one message; the child runs PR #36's
listener there (Silero VAD, utterance cutting, ECAPA identification on ONNX Runtime) and
answers with small results only. Nothing here ever reaches the Gemini connection or the
mic callback's thread.

The child is placed with vision_role "voice": the slow cores at nice 10, two ONNX threads.
"""
import collections
import threading
import time

from features.vision_process import Remote, Stopped

BATCH_S = 0.25            # how often queued audio goes to the child
MAX_QUEUED = 1500         # mic blocks kept while the child is busy (30 s at 20 ms)
RESTART_AFTER_S = 30.0    # a child that died is started again after this long
CALL_TIMEOUT_S = 20.0     # learning a voice embeds several clips in one call
ADOPT_FIRST = 3           # a first voice needs this many consistent clips before it is saved
ADOPT_POOL = 8            # ...found among this person's last this-many clips


class VoiceChild:
    """Runs in the child (features.vision_process)."""
    vision_role = "voice"
    raw_results = True
    device = "CPU"

    def __init__(self, sample_rate, people_path, model_dir=None):
        from features.person_recognition import faces, voices
        store = faces.PeopleStore(people_path)
        identifier = voices.VoiceIdentifier(voices.VoiceModel(model_dir), store)
        self.identifier = identifier
        for name, kept, before in identifier.recheck_voices():
            print(f"[VOICE] Rechecked {name}'s voice under the stricter learning rules: "
                  + (f"kept {kept} of {before} clips" if kept else
                     f"none of {before} clips sound like one speaker; it will be learned again"),
                  flush=True)
        self.results, self.learned, self.adopted = [], [], []
        self.recent = collections.OrderedDict()   # result id -> embedding, newest last
        self.pending = {}                          # person id -> [embeddings] for a first voice
        self.next_id = 0
        self.listener = voices.VoiceListener(identifier, sample_rate, on_result=self._result,
                                             vad=voices.make_vad(model_dir))

    def _result(self, match, stage):
        self.next_id += 1
        if match.embedding is not None and stage == "final":
            self.recent[self.next_id] = match.embedding
            while len(self.recent) > 20:
                self.recent.popitem(last=False)
        self.results.append({"level": match.level, "person_id": match.person_id, "name": match.name,
                             "score": round(match.score, 3), "seconds": round(match.seconds, 2),
                             "ms": round(match.ms), "stage": stage, "id": self.next_id})

    def _adopt(self, person_id, result_id):
        """Learn from conversation: the utterance `result_id` was probably said by `person_id`
        (the only face in view, recognised), though anyone in the room may have been talking.
        A first voice needs ADOPT_FIRST clips that clearly sound like one speaker
        (voices.consistent_core, ADOPT_CONSISTENCY); a later clip must sound like their
        stored voice (ADOPT_MATCH)."""
        from features.person_recognition import voices
        embedding = self.recent.pop(result_id, None)
        if embedding is None:
            return
        if self.identifier.has_voice(person_id):
            added = self.identifier.add_samples(person_id, [embedding], threshold=voices.ADOPT_MATCH)
            self.adopted.append({"person_id": person_id, "added": added, "first": False})
            return
        clips = (self.pending.get(person_id, []) + [embedding])[-ADOPT_POOL:]
        self.pending[person_id] = clips
        core = voices.consistent_core(clips, min_size=ADOPT_FIRST) if len(clips) >= ADOPT_FIRST else []
        if core:
            added = self.identifier.add_samples(person_id, [clips[i] for i in core],
                                                check_consistency=False)
            self.pending.pop(person_id, None)
            self.adopted.append({"person_id": person_id, "added": added, "first": True})
        else:
            self.adopted.append({"person_id": person_id, "added": 0, "collected": len(clips), "first": True,
                                 "note": "clips don't sound like one person yet" if len(clips) >= ADOPT_FIRST
                                 else None})

    def detect(self, item):
        command = item[0]
        if command == "audio":
            for chunk, ember_speaking, captured in item[1]:
                self.listener.process(chunk, ember_speaking, captured)
        elif command == "learn":
            _, name, allow_new = item
            self.listener.start_learning(name, self.learned.append, allow_new=allow_new)
        elif command == "adopt":
            _, person_id, result_id = item
            self._adopt(person_id, result_id)
        elif command != "poll":
            raise ValueError(f"unknown command {command!r}")
        reply = {"results": self.results, "learned": self.learned, "adopted": self.adopted,
                 "in_speech": self.listener._in_speech, "learning": self.listener.learning()}
        self.results, self.learned, self.adopted = [], [], []
        return reply


class VoiceFeed:
    """Ember's side: queue mic blocks, ship them to VoiceChild, keep the latest results."""

    def __init__(self, factory, on_result=None, on_learned=None, clock=time.monotonic, on_adopted=None):
        self.factory = factory              # () -> Remote (tests pass a fake)
        self.on_result = on_result or (lambda result: None)
        self.on_learned = on_learned or (lambda result: None)
        self.on_adopted = on_adopted or (lambda result: None)
        self.clock = clock
        self.queue = collections.deque(maxlen=MAX_QUEUED)   # append/popleft are thread-safe
        self.dropped = 0
        self.latest = None                  # (result dict, when)
        self.in_speech = False
        self.results = 0
        self.changed = threading.Condition()
        self.commands = collections.deque()
        self.stop = threading.Event()
        self.thread = None
        self.remote = None
        self.failed_at = None

    # ── Mic callback (PortAudio's thread): never blocks ────────────────────────
    def feed(self, chunk, ember_speaking):
        if len(self.queue) == MAX_QUEUED:
            self.dropped += 1            # the oldest block goes; audio to Gemini is unaffected
        self.queue.append((chunk, bool(ember_speaking), self.clock()))

    # ── Requests from tools ────────────────────────────────────────────────────
    def learn(self, name, allow_new=False):
        self.commands.append(("learn", name, allow_new))

    def adopt(self, person_id, result_id):
        """That utterance was this person's: learn their voice from it."""
        self.commands.append(("adopt", person_id, result_id))

    def speaker(self, wait=1.5, max_age=15.0):
        """The latest result, waiting up to `wait` s for an utterance still in progress."""
        with self.changed:
            start = self.results
            if self.in_speech or len(self.queue) > 10:
                self.changed.wait_for(lambda: self.results > start and not self.in_speech, timeout=wait)
            latest = self.latest
        if latest is None or self.clock() - latest[1] > max_age:
            return None
        return latest[0]

    # ── Feeder thread ──────────────────────────────────────────────────────────
    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.stop.clear()
            self.thread = threading.Thread(target=self._run, name="voice-feed", daemon=True)
            self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.remote is not None:
            self.remote.close()
            self.remote = None

    def _run(self):
        while not self.stop.wait(BATCH_S):
            if self.remote is None:
                if self.failed_at is not None and self.clock() - self.failed_at < RESTART_AFTER_S:
                    self.queue.clear()       # no child to send to: keep nothing stale
                    continue
                try:
                    self.remote = self.factory()
                    self.failed_at = None
                except Exception as error:
                    print(f"[VOICE] Voice recognition could not start: {error}", flush=True)
                    self.failed_at = self.clock()
                    continue
            try:
                self._step()
            except Stopped as error:
                print(f"[VOICE] Voice recognition process stopped ({error}); restarting later", flush=True)
                self.remote.close()
                self.remote, self.failed_at = None, self.clock()
            except Exception as error:     # one bad batch must not end recognition
                print(f"[VOICE] Error: {type(error).__name__}: {error}", flush=True)

    def _step(self):
        batch = []
        while self.queue:
            batch.append(self.queue.popleft())
        replies = []
        while self.commands:
            replies.append(self.remote.call(self.commands.popleft()))
        if batch:
            replies.append(self.remote.call(("audio", batch)))
        for reply in replies:
            with self.changed:
                for result in reply["results"]:
                    self.latest = (result, self.clock())
                    self.results += 1
                self.in_speech = reply["in_speech"]
                self.changed.notify_all()
            for result in reply["results"]:
                self.on_result(result)
            for result in reply["learned"]:
                self.on_learned(result)
            for result in reply.get("adopted", []):
                self.on_adopted(result)
