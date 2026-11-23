"""Face recognition's models in their own process, on the slow cores.

faces.FaceIdentifier (tracking, voting, the people store) stays in Ember's process: it is
small numpy work. Its models (YuNet detection, SFace embeddings, OpenCV DNN) run here,
behind the same interface as faces.FaceModels, so FaceIdentifier needs no change:

    models = RemoteFaceModels(paths)     # starts the child (features.vision_process)
    models.detect(frame, scale)          # sends the frame once; the child keeps it
    models.refine(frame, det)            # these three reuse the child's copy of that
    models.fingerprint(frame, det)       #   same frame: only the small detection
    models.embed(frame, det)             #   crosses the pipe
    models.close()

The child is placed like face landmarks (vision_role "face": the slow cores at nice 10)
and limited to one OpenCV thread, so it never competes with the microphone, the Gemini
connection or pose on the fast cores. A child that dies or hangs raises
vision_process.Stopped; the caller (people.People) closes it and starts another later.
"""
from features.vision_process import Remote


class FaceChild:
    """Runs in the child (features.vision_process): one frame at a time."""
    vision_role = "face"
    raw_results = True
    device = "CPU"

    def __init__(self, paths):
        from features.person_recognition import faces
        self.models = faces.FaceModels(paths)
        self.frame = None

    def detect(self, item):
        command = item[0]
        if command == "detect":
            _, frame, scale = item
            self.frame = frame
            return self.models.detect(frame, scale)
        if self.frame is None:
            return None
        det = item[1]
        if command == "refine":
            return self.models.refine(self.frame, det)
        if command == "fingerprint":
            return self.models.fingerprint(self.frame, det)
        if command == "embed":
            return self.models.embed(self.frame, det)
        raise ValueError(f"unknown command {command!r}")


class RemoteFaceModels:
    """faces.FaceModels' interface, answered by FaceChild in another process. The frame
    argument of refine/fingerprint/embed must be the one last passed to detect (as
    FaceIdentifier.process always does)."""

    def __init__(self, paths, remote=None):
        import functools
        self.remote = remote or Remote(functools.partial(FaceChild, paths))

    def detect(self, frame, scale=1.0):
        return self.remote.call(("detect", frame, scale))

    def refine(self, frame, det):
        return self.remote.call(("refine", det))

    def fingerprint(self, frame, det):
        return self.remote.call(("fingerprint", det))

    def embed(self, frame, det):
        return self.remote.call(("embed", det))

    def close(self):
        self.remote.close()
