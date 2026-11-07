"""Run a camera model (pose or face landmarks) in its own process.

In Ember's main process MediaPipe inference held Python's GIL, so the microphone
callback and the Gemini connection waited behind every frame: mic overflows and
replies up to a minute late on the Orange Pi. A child process has its own GIL and
cores. Only frames go in and plain landmark tuples come back over its pipes.

The core camera thread's pose child runs features.pose.backend.ChildPose: on an
Orange Pi 5 (RK3588) that passed the NPU check it drives the NPU (RKNN) from here,
else MediaPipe on the CPU. A crash in the NPU runtime ends this child, not Ember.
Where Ember runs on the RK3588's fast cores, the child places itself before it builds
its model (pc_hardware.place_vision_child): pose stays on the fast cores at nice 5,
face landmarks go to the slow cores at nice 10.

    model = Remote(FaceLandmarks)      # any picklable factory with .detect(frame)
    landmarks = model.detect(frame)    # list[Point] or None, as the local model
    model.device                       # the model's .device after its last frame
                                       # ("NPU"/"CPU"), or None if it has none

Run as `python -m features.vision_process`; Remote starts it, nothing else should.
"""
import collections
import os
import pickle
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
Point = collections.namedtuple("Point", "x y z visibility presence")


def points(landmarks):
    """MediaPipe landmarks as plain tuples (x/y/z/visibility read like the originals)."""
    if landmarks is None:
        return None
    return [Point(p.x, p.y, p.z, p.visibility or 0.0, p.presence or 0.0) for p in landmarks]


class Remote:
    """`factory()` built and run in a child process, with the same detect(frame) call."""

    def __init__(self, factory):
        self.lock = threading.Lock()
        self.child = subprocess.Popen([sys.executable, "-m", "features.vision_process"], cwd=ROOT,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.device = self._call(factory)
        except Exception:
            self.close()
            raise

    def _call(self, item):
        with self.lock:
            try:
                pickle.dump(item, self.child.stdin, protocol=pickle.HIGHEST_PROTOCOL)
                self.child.stdin.flush()
                kind, value = pickle.load(self.child.stdout)
            except (EOFError, OSError, pickle.UnpicklingError) as error:
                raise RuntimeError(f"vision process stopped ({error or 'exited'})") from error
            except Exception as error:
                # Anything else on the reply pipe (e.g. text, which unpickles as a ValueError):
                # replies are out of step from here on, so callers close it as a stopped process.
                raise RuntimeError(f"vision process sent an unreadable reply ({type(error).__name__}: {error})") \
                    from error
        if kind == "error":
            raise RuntimeError(value)
        return value

    def detect(self, frame):
        landmarks, self.device = self._call(frame)
        return landmarks

    def close(self):
        try:
            self.child.stdin.close()  # The child exits at end of input.
        except OSError:
            pass
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _place(factory):
    """This child's cores and priority, set before the model starts any thread (they inherit
    both): pc_hardware.place_vision_child, which changes nothing unless Ember runs on the fast
    cores. A model class with vision_role = "face" (FaceLandmarks) is placed as face
    landmarks, anything else as pose. Placement only saves time: an error never stops the model."""
    role = "face" if getattr(getattr(factory, "func", factory), "vision_role", None) == "face" else "pose"
    try:
        import pc_hardware
        if pc_hardware.place_vision_child(role):
            import cv2
            cv2.setNumThreads(1)  # one colour conversion or crop a frame: no OpenCV pool spinning beside it
    except Exception as error:
        print(f"[VISION] Core placement skipped ({type(error).__name__}: {error})", flush=True)


def main():
    requests = sys.stdin.buffer
    # Replies get their own copy of the pipe; descriptor 1 then points at stderr, so
    # native libraries writing to C stdout (e.g. librknnrt's "I RKNN: ..." lines) and
    # Python prints go to Ember's log instead of corrupting the reply pipe.
    replies = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except OSError:
        pass  # No usable stderr: native writes to fd 1 stay a risk, Python prints do not.
    sys.stdout = sys.stderr

    def reply(kind, value):
        pickle.dump((kind, value), replies, protocol=pickle.HIGHEST_PROTOCOL)
        replies.flush()

    try:
        factory = pickle.load(requests)
        _place(factory)  # first: the model's threads inherit the cores and the nice value
        model = factory()
    except Exception as error:
        reply("error", f"{type(error).__name__}: {error}")
        return
    reply("ok", getattr(model, "device", None))
    try:
        while True:
            try:
                frame = pickle.load(requests)
            except EOFError:
                return
            try:
                landmarks = points(model.detect(frame))
                # Read after detect: an NPU failure switches to the CPU inside it.
                reply("ok", (landmarks, getattr(model, "device", None)))
            except Exception as error:
                reply("error", f"{type(error).__name__}: {error}")
    finally:
        try:
            if hasattr(model, "close"):
                model.close()  # e.g. release the NPU (RKNN) cleanly
        except Exception:
            pass


if __name__ == "__main__":
    # Run the imported module, not __main__, so Point pickles as features.vision_process.Point.
    from features.vision_process import main as serve
    serve()
