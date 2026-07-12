from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "code" / "s11_error_recovery.py"

if "anthropic" not in sys.modules:
    anthropic = types.ModuleType("anthropic")

    class Anthropic:  # type: ignore[no-redef]
        def __init__(self, **_kwargs: object) -> None:
            self.messages = Mock()

    anthropic.Anthropic = Anthropic
    sys.modules["anthropic"] = anthropic

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda **_kwargs: None
    sys.modules["dotenv"] = dotenv

SPEC = importlib.util.spec_from_file_location("s11_error_recovery", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
s11 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s11)


class ErrorRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        s11.ERROR_HISTORY.clear()

    def test_classify_error_prefers_specific_categories(self) -> None:
        self.assertEqual(s11.classify_error("Permission denied: request timed out"), "permission")
        self.assertEqual(s11.classify_error("FileNotFoundError: no such file"), "not_found")
        self.assertEqual(s11.classify_error("ValidationError: required field"), "validation")
        self.assertEqual(s11.classify_error("HTTP 429 service unavailable"), "transient")
        self.assertEqual(s11.classify_error("connection reset by peer"), "transient")

    def test_error_history_keeps_only_recent_records(self) -> None:
        for index in range(s11.MAX_ERROR_HISTORY + 3):
            s11.record_error("test", f"Error: {index}")

        self.assertEqual(len(s11.ERROR_HISTORY), s11.MAX_ERROR_HISTORY)
        self.assertEqual(s11.ERROR_HISTORY[0]["message"], "Error: 3")

    def test_attach_recovery_hint_records_common_failures(self) -> None:
        output = s11.attach_recovery_hint("bash", "(exit code 1; no output)")

        self.assertIn("[error_recovery]", output)
        self.assertEqual(len(s11.ERROR_HISTORY), 1)
        self.assertEqual(s11.ERROR_HISTORY[0]["where"], "bash")

    def test_run_bash_silent_nonzero_exit_is_error_shape(self) -> None:
        output = s11.run_bash("false")

        self.assertTrue(output.startswith("(exit code 1"))
        self.assertTrue(s11.is_error_output(output))

    def test_non_transient_model_error_does_not_retry(self) -> None:
        client = Mock()
        client.messages.create.side_effect = ValueError("invalid request")

        with patch.object(s11, "client", client), patch.object(s11.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                s11.call_model_with_retries(model="test")

        self.assertEqual(client.messages.create.call_count, 1)
        sleep.assert_not_called()

    def test_transient_model_error_retries_up_to_limit(self) -> None:
        client = Mock()
        client.messages.create.side_effect = RuntimeError("HTTP 429 rate limit")

        with patch.object(s11, "client", client), patch.object(s11.time, "sleep") as sleep, patch.object(
            s11.random, "uniform", return_value=0
        ):
            with self.assertRaises(RuntimeError):
                s11.call_model_with_retries(model="test")

        self.assertEqual(client.messages.create.call_count, s11.MODEL_RETRY_ATTEMPTS)
        self.assertEqual(sleep.call_count, s11.MODEL_RETRY_ATTEMPTS - 1)

    def test_permanent_error_after_transient_stops_immediately(self) -> None:
        client = Mock()
        client.messages.create.side_effect = [
            RuntimeError("HTTP 429 rate limit"),
            ValueError("invalid request"),
        ]

        with patch.object(s11, "client", client), patch.object(s11.time, "sleep"), patch.object(
            s11.random, "uniform", return_value=0
        ):
            with self.assertRaises(RuntimeError):
                s11.call_model_with_retries(model="test")

        self.assertEqual(client.messages.create.call_count, 2)

    def test_subagent_cannot_view_main_error_history(self) -> None:
        self.assertNotIn("show_errors", s11.SUBAGENT_TOOL_HANDLERS)
        self.assertNotIn("show_errors", {tool["name"] for tool in s11.SUBAGENT_TOOLS})


if __name__ == "__main__":
    unittest.main()
