import contextlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
from features.conversational_ability import live_models
from gemini_preflight import PreflightError, configured_models, validate_key
import run_pc

RUNTIME = Path(__file__).resolve().parents[1] / "realtime_gemini_8.py"
KEY = "fake-key-not-a-real-credential"


class GeminiPreflightTests(unittest.TestCase):
    def session(self, responses=None, error=None):
        session = Mock()
        if error is not None:
            session.get.side_effect = error
        elif responses is not None:
            session.get.side_effect = responses
        else:
            session.get.return_value = SimpleNamespace(status_code=200, json=lambda: {"name": "models/example"})
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=session)
        factory.return_value.__exit__ = Mock(return_value=False)
        return factory, session

    def test_uses_actual_models_header_timeout_and_no_redirects(self):
        factory, session = self.session()
        models = validate_key(KEY, RUNTIME, factory)
        self.assertEqual(models, configured_models(RUNTIME))
        self.assertEqual(session.get.call_count, 2)
        for call, model in zip(session.get.call_args_list, models):
            self.assertEqual(call.args[0], "https://generativelanguage.googleapis.com/v1beta/models/" + model)
            self.assertNotIn(KEY, call.args[0])
            self.assertEqual(call.kwargs["headers"], {"x-goog-api-key": KEY})
            self.assertEqual(call.kwargs["timeout"], (5, 10))
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_blank_or_malformed_keys_never_contact_google(self):
        factory = Mock()
        for key in ("", "two words", "line\nbreak", None):
            with self.assertRaises(PreflightError):
                validate_key(key, RUNTIME, factory)
        factory.assert_not_called()

    def test_provider_errors_are_actionable_and_do_not_echo_raw_key(self):
        for code, text in ((400, "rejected"), (401, "rejected"), (403, "denied"),
                           (404, "model"), (429, "quota"), (503, "unavailable"), (302, "unexpected")):
            response = SimpleNamespace(status_code=code, json=lambda: {"error": {"message": KEY}})
            factory, session = self.session([response])
            with self.assertRaises(PreflightError) as error:
                validate_key(KEY, RUNTIME, factory)
            self.assertIn(text, str(error.exception))
            self.assertNotIn(KEY, str(error.exception))
            self.assertEqual(session.get.call_count, 1)

    def test_timeout_and_network_errors_do_not_echo_exception_details(self):
        for exc, word in ((requests.Timeout(KEY), "timed out"), (requests.ConnectionError(KEY), "internet")):
            factory, _ = self.session(error=exc)
            with self.assertRaises(PreflightError) as error:
                validate_key(KEY, RUNTIME, factory)
            self.assertIn(word, str(error.exception))
            self.assertNotIn(KEY, str(error.exception))

    def test_success_status_with_bad_metadata_is_not_a_pass(self):
        for payload in ({}, [], {"name": 10}):
            factory, _ = self.session([SimpleNamespace(status_code=200, json=lambda: payload)])
            with self.assertRaises(PreflightError):
                validate_key(KEY, RUNTIME, factory)
        response = SimpleNamespace(status_code=200, json=Mock(side_effect=ValueError(KEY)))
        factory, _ = self.session([response])
        with self.assertRaises(PreflightError) as error:
            validate_key(KEY, RUNTIME, factory)
        self.assertNotIn(KEY, str(error.exception))

    # ── v10's Live models (BAYMAX_LIVE_MODELS): one this key can use is enough ──
    def listed(self, *codes):
        """validate_key with the Live models a and b in MODEL's place; Google answers
        each request with the next code (200 lists the model)."""
        responses = [SimpleNamespace(status_code=code, json=lambda: {"name": "models/m"}) for code in codes]
        factory, session = self.session(responses)
        asked = []
        result = validate_key(KEY, RUNTIME, factory, live_models=lambda model: asked.append(model) or ("a", "b"))
        self.assertEqual(asked, [configured_models(RUNTIME)[0]])  # given the runtime's MODEL
        return result, [call.args[0].rsplit("/", 1)[1] for call in session.get.call_args_list]

    def test_a_live_model_google_does_not_list_for_the_key_is_passed_over(self):
        summary = configured_models(RUNTIME)[1]
        self.assertEqual(self.listed(404, 200, 200), (("b", summary), ["a", "b", summary]))
        # The first one listed is enough: no request (and no wait) for the rest.
        self.assertEqual(self.listed(200, 200), (("a", summary), ["a", summary]))

    def test_startup_stops_when_the_key_can_use_no_live_model(self):
        with self.assertRaises(PreflightError) as error:
            self.listed(404, 404)
        self.assertIn("(a, b)", str(error.exception))
        self.assertIn("BAYMAX_LIVE_MODELS", str(error.exception))
        for code, text in ((401, "rejected"), (403, "denied"), (429, "quota"), (503, "unavailable")):
            with self.subTest(code=code), self.assertRaises(PreflightError) as error:
                self.listed(code)  # anything but "not found" stops startup at once, as before
            self.assertIn(text, str(error.exception))

    def test_second_model_failure_also_stops_startup(self):
        factory, session = self.session([SimpleNamespace(status_code=200, json=lambda: {"name": "models/live"}),
                                        SimpleNamespace(status_code=403)])
        with self.assertRaises(PreflightError):
            validate_key(KEY, RUNTIME, factory)
        self.assertEqual(session.get.call_count, 2)

    def startup(self, key=KEY, error=None, email=""):
        output = io.StringIO()
        with patch.dict(os.environ, {"GOOGLE_API_KEY": key, "BAYMAX_EMAIL_FROM": email}, clear=True), patch.object(
                run_pc, "load_dotenv"), patch.object(run_pc.os, "chdir"), patch.object(
                run_pc, "validate_key", side_effect=error) as validate, patch.object(
                run_pc.runpy, "run_path") as runtime, patch.object(run_pc, "use_performance_cpus") as pin, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            order = Mock()
            order.attach_mock(pin, "pin")
            order.attach_mock(validate, "validate")
            order.attach_mock(runtime, "runtime")
            result = run_pc.main()
        return result, output.getvalue(), validate, runtime, order

    def test_invalid_key_stops_before_loading_hardware_runtime(self):
        result, output, validate, runtime, _ = self.startup(error=PreflightError("Google rejected the key."))
        self.assertEqual(result, 1)
        validate.assert_called_once()
        runtime.assert_not_called()
        self.assertNotIn(KEY, output)

    def test_missing_key_or_production_credentials_do_not_contact_google(self):
        for options in ({"key": ""}, {"email": "example@example.invalid"}):
            result, output, validate, runtime, _ = self.startup(**options)
            self.assertEqual(result, 1)
            validate.assert_not_called()
            runtime.assert_not_called()
            self.assertNotIn(KEY, output)

    def test_startup_checks_the_live_models_v10_will_try(self):
        _, _, validate, _, _ = self.startup()
        self.assertIs(validate.call_args.kwargs["live_models"], live_models.configured)

    def test_success_loads_runtime_only_after_validation(self):
        result, output, _, _, order = self.startup()
        self.assertEqual(result, 0)
        # Fast-core pinning (pc_hardware) comes first, before any thread or the runtime starts.
        self.assertEqual([call[0] for call in order.mock_calls], ["pin", "validate", "runtime"])
        self.assertIn("Google accepted the key", output)
        self.assertNotIn(KEY, output)
