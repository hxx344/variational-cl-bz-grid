import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from variational_grid.cli import configuration, main, run
from variational_grid.models import Config, GridError
from variational_grid.store import Store


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "config.json"
        self.config.write_text(json.dumps(asdict(Config())), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_relative_paths_follow_config_not_cwd(self):
        config = configuration(self.config)
        self.assertEqual(Path(config.state_file), (self.config.parent / "data/paper.sqlite3").resolve())
        self.assertEqual(Path(config.session_file), (self.config.parent / "data/session.json").resolve())

    def test_configuration_rejects_overlapping_runtime_files(self):
        for changes in ({"state_file": "data/./session.json"}, {"session_file": "config.json"}, {"state_file": ""}):
            self.config.write_text(json.dumps({**asdict(Config()), **changes}), encoding="utf-8")
            with self.assertRaises(GridError):
                configuration(self.config)

    def test_missing_history_pauses_without_fetching_quotes(self):
        class FakeClient:
            def __init__(self, *_):
                pass
            def check_session(self):
                return {"authenticated": True}
            def candles(self, symbol, end):
                return []
            def quote(self, *_):
                raise AssertionError("Quote should not be requested without history")
        with patch("variational_grid.cli.Client", FakeClient), redirect_stdout(io.StringIO()) as output:
            code = run(argparse.Namespace(config=self.config, once=True, iterations=0))
        self.assertEqual(code, 2)
        result = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(result["status"], "paused")
        store = Store(configuration(self.config).state_file, Config())
        try:
            self.assertIsNone(store.snapshot())
            self.assertEqual(store.lots(), [])
        finally:
            store.close()

    def test_unknown_exception_does_not_print_secret_traceback(self):
        with patch("variational_grid.cli.Client.check_session", side_effect=RuntimeError("PRIVATE-SECRET")), redirect_stdout(io.StringIO()) as output:
            code = main(["run", "--config", str(self.config), "--once"])
        self.assertEqual(code, 2)
        self.assertNotIn("PRIVATE-SECRET", output.getvalue())

    def test_session_check_missing_file_is_a_clean_error_without_network(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Missing session must fail before network")), redirect_stdout(io.StringIO()) as output:
            code = main(["check-session", "--config", str(self.config)])
        self.assertEqual(code, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertIn("Cannot read session", result["reason"])
        self.assertNotIn("Traceback", output.getvalue())

    def test_session_check_success_reports_only_verified_status(self):
        verified = {"authenticated": True, "expires_utc": "2030-01-01T00:00:00+00:00"}
        with patch("variational_grid.cli.Client.check_session", return_value=verified), redirect_stdout(io.StringIO()) as output:
            code = main(["check-session", "--config", str(self.config)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), verified)

    def test_session_check_unexpected_failure_does_not_escape_to_excepthook(self):
        with patch("variational_grid.cli.Client.check_session", side_effect=RuntimeError("PRIVATE-SECRET")), redirect_stdout(io.StringIO()) as output:
            code = main(["check-session", "--config", str(self.config)])
        self.assertEqual(code, 2)
        self.assertNotIn("PRIVATE-SECRET", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_export_refuses_overwriting_session_config_or_state(self):
        config = configuration(self.config)
        for path in (self.config, config.session_file, config.state_file):
            with redirect_stdout(io.StringIO()) as output:
                result = main(["export", "--config", str(self.config), "--output", str(path)])
            self.assertEqual(result, 2)
            self.assertIn("Export destination", output.getvalue())

    def test_demo_does_not_require_client_or_overwrite_existing_ledger(self):
        ledger = Path(self.temp.name) / "demo.sqlite3"
        with patch("variational_grid.cli.Client", side_effect=AssertionError("No network in demo")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["demo", "--state-file", str(ledger)]), 0)
        result = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(result["demo"], "synthetic_scenario_not_backtest")
        self.assertEqual(result["open_pairs"], 0)
        before = ledger.read_bytes()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["demo", "--state-file", str(ledger)]), 2)
        self.assertEqual(ledger.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
