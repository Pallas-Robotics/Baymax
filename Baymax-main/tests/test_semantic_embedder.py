"""Memory's torch-free embedder: model download checks, tokenizer/ONNX wiring, pooling,
and the runtime's first-start download. Fakes stand in for onnxruntime and tokenizers,
so this runs on profiles without the memory packages (the desktop ones)."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

import semantic_embedder
from test_public_core_integration import load_function

ROOT = Path(__file__).resolve().parents[1]
V10 = ROOT / "realtime_gemini_10.py"


def fake_files(contents):
    """MODEL_FILES entries for the given bytes, plus an urlopen serving them."""
    files = {name: (f"remote/{name}", len(data), hashlib.sha256(data).hexdigest())
             for name, data in contents.items()}
    served = {f"remote/{name}": data for name, data in contents.items()}

    def urlopen(url, timeout):
        return io.BytesIO(served[url.split("/", 1)[1]])
    return files, urlopen


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.url = patch.object(semantic_embedder, "MODEL_URL", "x/{path}")
        self.url.start()
        self.addCleanup(self.url.stop)

    def download(self, files, urlopen):
        with patch.object(semantic_embedder, "MODEL_FILES", files), \
                patch.object(semantic_embedder.urllib.request, "urlopen", urlopen):
            semantic_embedder.download_model(self.dir)

    def test_checked_files_are_placed_model_last(self):
        files, urlopen = fake_files({"tokenizer.json": b"{}", "model.onnx": b"onnx" * 1000})
        self.download(files, urlopen)
        self.assertEqual((self.dir / "model.onnx").read_bytes(), b"onnx" * 1000)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["model.onnx", "tokenizer.json"])
        if os.name == "posix":  # readable by a service running as another user
            self.assertEqual((self.dir / "model.onnx").stat().st_mode & 0o777, 0o644)
        with patch.object(semantic_embedder, "MODEL_FILES", files):
            self.assertTrue(semantic_embedder.model_present(self.dir))
        self.assertEqual(list(semantic_embedder.MODEL_FILES), ["tokenizer.json", "model.onnx"])

    def test_wrong_checksum_or_size_leaves_no_model(self):
        files, _ = fake_files({"tokenizer.json": b"{}", "model.onnx": b"good"})
        for served in (b"evil", b"good+"):
            with self.subTest(served=served):
                def urlopen(url, timeout):
                    return io.BytesIO(b"{}" if url.endswith("tokenizer.json") else served)
                with self.assertRaisesRegex(RuntimeError, "failed its check"):
                    self.download(files, urlopen)
                self.assertEqual([p.name for p in self.dir.iterdir()], ["tokenizer.json"])
                with patch.object(semantic_embedder, "MODEL_FILES", files):
                    self.assertFalse(semantic_embedder.model_present(self.dir))

    def test_oversized_download_stops_early(self):
        files, _ = fake_files({"model.onnx": b"x" * 10})
        reads = []

        class Endless(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return b"x" * size
        with self.assertRaisesRegex(RuntimeError, "failed its check"):
            self.download(files, lambda url, timeout: Endless())
        self.assertEqual(len(reads), 1)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_network_error_removes_the_partial_file(self):
        files, _ = fake_files({"model.onnx": b"x"})

        def urlopen(url, timeout):
            raise OSError("offline")
        with self.assertRaises(OSError):
            self.download(files, urlopen)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_old_partial_file_is_removed_but_not_one_being_written(self):
        old, current = self.dir / ".model.onnx.abc123", self.dir / ".model.onnx.def456"
        old.write_bytes(b"half")
        current.write_bytes(b"another start's download")
        an_hour_ago = time.time() - 3600
        os.utime(old, (an_hour_ago, an_hour_ago))
        files, urlopen = fake_files({"model.onnx": b"whole"})
        self.download(files, urlopen)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".model.onnx.def456", "model.onnx"])

    def test_data_is_synced_before_the_rename(self):
        files, urlopen = fake_files({"model.onnx": b"whole"})
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with patch.object(semantic_embedder.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))), \
                patch.object(semantic_embedder.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))):
            self.download(files, urlopen)
        self.assertEqual(order, ["fsync", "replace"])

    def test_cleanup_keeps_the_original_error_when_the_partial_file_is_gone(self):
        files, _ = fake_files({"model.onnx": b"x"})

        def urlopen(url, timeout):
            for partial in self.dir.glob(".model.onnx.*"):
                partial.unlink()  # e.g. removed by hand meanwhile
            raise OSError("offline")
        with self.assertRaisesRegex(OSError, "offline"):
            self.download(files, urlopen)

    def test_pinned_revision_and_hashes(self):
        self.assertRegex(semantic_embedder.MODEL_REVISION, r"^[0-9a-f]{40}$")
        for path, size, sha256 in semantic_embedder.MODEL_FILES.values():
            self.assertRegex(sha256, r"^[0-9a-f]{64}$")
            self.assertGreater(size, 0)


class FakeEncoding:
    def __init__(self, ids, length):
        pad = length - len(ids)
        self.ids = ids + [0] * pad
        self.attention_mask = [1] * len(ids) + [0] * pad
        self.type_ids = [0] * length


class FakeTokenizer:
    def __init__(self, path):
        self.path, self.truncation, self.padding = path, None, None

    @classmethod
    def from_file(cls, path):
        return cls(path)

    def enable_truncation(self, max_length):
        self.truncation = max_length

    def enable_padding(self, pad_id, pad_token):
        self.padding = (pad_id, pad_token)

    def token_to_id(self, token):
        return {"[PAD]": 0}[token]

    def encode_batch(self, texts):
        ids = [[101] + [len(word) for word in text.split()] + [102] for text in texts]
        longest = max(map(len, ids))
        return [FakeEncoding(i, longest) for i in ids]


class FakeSession:
    """last_hidden_state where token t of every text is the vector [id_t] * 384."""
    def __init__(self, path, options, providers):
        self.path, self.options, self.providers, self.feeds = path, options, providers, []

    def get_inputs(self):
        return [SimpleNamespace(name=n) for n in ("input_ids", "attention_mask")]

    def run(self, outputs, feed):
        self.feeds.append(feed)
        assert outputs == ["last_hidden_state"]
        return [np.repeat(feed["input_ids"][..., None].astype(np.float32), 384, axis=2)]


class FakeOptions:
    def __init__(self):
        self.config = {}

    def add_session_config_entry(self, key, value):
        self.config[key] = value


class EmbedderTests(unittest.TestCase):
    def setUp(self):
        ort = ModuleType("onnxruntime")
        ort.SessionOptions, ort.InferenceSession = FakeOptions, FakeSession
        tokenizers = ModuleType("tokenizers")
        tokenizers.Tokenizer = FakeTokenizer
        modules = patch.dict(sys.modules, {"onnxruntime": ort, "tokenizers": tokenizers})
        modules.start()
        self.addCleanup(modules.stop)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for name in ("model.onnx", "tokenizer.json"):
            Path(self.dir, name).write_bytes(b"")

    def embedder(self):
        return semantic_embedder.SemanticEmbedder(model_dir=self.dir, verbose=False, chroma_dir=None)

    def test_loads_without_torch_and_configures_tokenizer_and_session(self):
        with patch.dict(sys.modules, {"torch": None, "transformers": None, "optimum": None}):
            embedder = self.embedder()
        self.assertEqual(embedder.tokenizer.truncation, 256)
        self.assertEqual(embedder.tokenizer.padding, (0, "[PAD]"))
        options = embedder.model.options
        self.assertEqual(options.intra_op_num_threads, min(2, os.cpu_count() or 1))
        self.assertEqual(options.inter_op_num_threads, 1)
        self.assertEqual(options.config["session.intra_op.allow_spinning"], "0")
        self.assertEqual(embedder.model.providers, ["CPUExecutionProvider"])

    def test_mean_of_real_tokens_ignoring_padding(self):
        embedder = self.embedder()
        out = embedder.embed(["abc de", "abcdefgh"])  # ids [101,3,2,102] and [101,8,102,0(pad)]
        self.assertEqual(out.shape, (2, 384))
        self.assertEqual(out.dtype, np.float32)
        np.testing.assert_allclose(out[:, 0], [(101 + 3 + 2 + 102) / 4, (101 + 8 + 102) / 3], rtol=1e-6)
        # Only the inputs the model declares are fed (this one has no token_type_ids).
        self.assertEqual(sorted(embedder.model.feeds[-1]), ["attention_mask", "input_ids"])
        self.assertEqual(embedder.model.feeds[-1]["input_ids"].dtype, np.int64)

    def test_single_string_is_a_batch_of_one(self):
        self.assertEqual(self.embedder().embed("hello").shape, (1, 384))

    def test_incomplete_model_dir_is_reported(self):
        os.remove(Path(self.dir, "tokenizer.json"))
        with self.assertRaisesRegex(FileNotFoundError, "tokenizer.json"):
            self.embedder()

    def test_missing_packages_are_named_without_importing(self):
        absent = {"tokenizers", "chromadb"}
        with patch.object(semantic_embedder.importlib.util, "find_spec",
                          side_effect=lambda name: None if name in absent else object()):
            self.assertEqual(semantic_embedder.missing_packages(), ["tokenizers", "chromadb"])


class RuntimeLoadTests(unittest.TestCase):
    """realtime_gemini_10: the model is fetched in the background on a first start, only
    where memory's packages are installed; startup never waits for it."""
    def namespace(self, missing=(), present=False, download_error=None):
        calls = []

        def download(path):
            calls.append(("download", path))
            if download_error:
                raise download_error
        ns = {
            "missing_packages": lambda: list(missing),
            "model_present": lambda path: present,
            "download_model": download,
            "SemanticEmbedder": lambda **kw: calls.append(("load", kw["model_dir"]))
                                           or SimpleNamespace(_collection=None),
            "ONNX_MODEL_DIR": "/models/minilm", "CHROMA_DIR": "/data/chroma",
            "_memory_embedder": None, "calls": calls,
            "monitor": SimpleNamespace(memory_status="Disabled"),
        }
        with patch("test_public_core_integration.CORE", V10):
            for name in ("_load_memory_embedder", "_memory_model_to_download", "_download_memory_model"):
                load_function(name, ns)
        return ns

    def test_only_a_missing_model_with_the_packages_installed_is_downloaded(self):
        self.assertTrue(self.namespace()["_memory_model_to_download"]())
        self.assertFalse(self.namespace(present=True)["_memory_model_to_download"]())
        self.assertFalse(self.namespace(missing=["chromadb"])["_memory_model_to_download"]())

    def test_background_download_then_memory_turns_on(self):
        ns = self.namespace()
        with patch("builtins.print"):
            ns["_download_memory_model"]()
        self.assertEqual(ns["calls"], [("download", "/models/minilm"), ("load", "/models/minilm")])
        self.assertIsNotNone(ns["_memory_embedder"])
        self.assertEqual(ns["monitor"].memory_status, "On (0 memories at start)")  # what the monitor shows

    def test_failed_download_leaves_memory_off_for_the_session(self):
        ns = self.namespace(download_error=OSError("offline"))
        with patch("builtins.print") as printed:
            ns["_download_memory_model"]()
        self.assertEqual(ns["calls"], [("download", "/models/minilm")])
        self.assertIsNone(ns["_memory_embedder"])
        self.assertIn("retried next start", " ".join(str(c) for c in printed.call_args_list))
        self.assertIn("download failed", ns["monitor"].memory_status)

    def test_startup_starts_the_download_on_a_thread_instead_of_waiting(self):
        source = V10.read_text(encoding="utf-8")
        main = source[source.index('if __name__ == "__main__":'):]
        self.assertIn("threading.Thread(target=_download_memory_model", main)
        self.assertNotIn("download_model(", main)


@unittest.skipUnless(os.getenv("BAYMAX_MEMORY_MODEL_DIR"), "set BAYMAX_MEMORY_MODEL_DIR to a real model dir")
class RealModelTests(unittest.TestCase):
    """With the real model and packages installed (e.g. on the Orange Pi)."""
    def test_similar_texts_rank_first_and_vectors_match_the_model(self):
        embedder = semantic_embedder.SemanticEmbedder(
            model_dir=os.environ["BAYMAX_MEMORY_MODEL_DIR"], verbose=False, chroma_dir=None)
        sims = embedder.batch_similarity(["The cat sits on the mat"],
                                         ["A cat is on the mat", "Stock prices fell sharply"])
        self.assertGreater(sims[0, 0], 0.8)
        self.assertLess(sims[0, 1], 0.2)
        one = embedder.embed("Hello world")
        self.assertEqual(one.shape, (1, 384))
        # Padding in a batch doesn't change a text's vector.
        np.testing.assert_allclose(embedder.embed(["Hello world", "a much longer text " * 20])[0],
                                   one[0], atol=1e-5)

    def test_same_vectors_as_the_torch_embedder_including_a_truncated_text(self):
        # First values from the previous optimum + transformers + torch embedder.
        embedder = semantic_embedder.SemanticEmbedder(
            model_dir=os.environ["BAYMAX_MEMORY_MODEL_DIR"], verbose=False, chroma_dir=None)
        long = "My granddaughter Lily visits every Sunday and we bake bread together. " * 30  # 392 tokens
        out = embedder.embed(["Hello world", long])
        np.testing.assert_allclose(out[0, :6], [-0.197446, 0.177665, 0.03857, 0.149522, -0.22542, -0.918028], atol=2e-5)
        np.testing.assert_allclose(out[1, :6], [0.051162, -0.088427, 0.175685, -0.033214, -0.294623, 0.082958], atol=2e-5)


if __name__ == "__main__":
    unittest.main()
