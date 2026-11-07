"""Execute real config/receive hooks without importing devices, models or Gemini."""
import ast
import asyncio
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from features.toolkits import experimental

CORE = Path(__file__).resolve().parents[1] / "realtime_gemini_10.py"


def load_function(name, namespace):
    tree = ast.parse(CORE.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, str(CORE), "exec"), namespace)
    return namespace[name]


class CoreIntegrationTests(unittest.TestCase):
    def test_real_config_hook_disabled_preserves_base(self):
        base = {"response_modalities": ["AUDIO"], "speech_config": {"voice": "unchanged"}}
        build = load_function("_build_config", {"_BASE_CONFIG": base, "_BASE_SYSTEM_INSTRUCTION": "base",
                                               "_memory_embedder": None, "public_toolkit": experimental})
        with patch.dict(os.environ, {"EMBER_PUBLIC_TOOLKIT": "0", "EMBER_DISCOVERY_ENABLED": "0"}):
            self.assertEqual(build(), {**base, "system_instruction": "base"})

    def test_real_config_hook_enabled_keeps_original_settings(self):
        base = {"speech_config": {"voice": "unchanged"}, "realtime_input_config": {"sample": "unchanged"}}
        build = load_function("_build_config", {"_BASE_CONFIG": base, "_BASE_SYSTEM_INSTRUCTION": "base",
                                               "_memory_embedder": None, "public_toolkit": experimental})
        with patch.dict(os.environ, {"EMBER_PUBLIC_TOOLKIT": "1", "EMBER_PUBLIC_TOOL_GROUPS": "feeds"}):
            result = build()
        self.assertEqual(result["speech_config"], base["speech_config"])
        self.assertEqual(result["realtime_input_config"], base["realtime_input_config"])
        self.assertTrue(result["system_instruction"].startswith("base"))

    def test_real_receive_loop_dispatches_before_missing_server_content(self):
        calls = SimpleNamespace(function_calls=[SimpleNamespace(name="public_capabilities", args={}, id="x")])
        class Session:
            count = 0
            def receive(self):
                async def stream():
                    self.count += 1
                    if self.count > 1:
                        raise asyncio.CancelledError()
                    yield SimpleNamespace(tool_call=calls, server_content=None,
                                          session_resumption_update=None, go_away=None)
                return stream()
        handler = AsyncMock()
        receive = load_function("receive_audio", {"asyncio": asyncio, "time": time,
            "_TurnTranscript": lambda *a: SimpleNamespace(flush=lambda: None),
            "monitor": SimpleNamespace(timing=Mock()),
            "public_toolkit": SimpleNamespace(handle_tool_call=handler),
            "_address_gate": SimpleNamespace(tool_call=lambda: None)})
        session = Session()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(receive(session))
        handler.assert_awaited_once_with(session, calls)

    def test_container_allowlist_includes_toolkit_python_only(self):
        rules = (CORE.parent / ".dockerignore").read_text()
        self.assertIn("!features/toolkits/", rules)
        self.assertIn("!features/toolkits/*.py", rules)
        self.assertNotIn("!toolkit_feed_preferences.json", rules)


if __name__ == "__main__":
    unittest.main()
