"""Current state stays available while historical reads fail, block or reset."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_qqq_history as fixtures
from variational_grid.dashboard import make_server
from variational_grid.models import GridError
from variational_grid.qqq_comparison import decode_summary, read_qqq_snapshot, read_qqq_history, summary_record
from variational_grid.qqq_history import HistoryTimeout, read_history
from variational_grid.reset import save_state


class LoadingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.experiment = fixtures.HistoryTests.detail_fixture(self, Path(folder.name))
        self.idle = {"generation": "old", "status": "idle"}
        save_state(self.experiment, self.idle)

    def test_snapshot_does_not_read_history_or_wait_for_control_lock(self):
        from variational_grid.reset import control_lock
        with control_lock(self.experiment), patch("variational_grid.qqq_history.read_history", side_effect=AssertionError("No history")):
            snapshot = read_qqq_snapshot(self.experiment)
        self.assertEqual(snapshot["summary"]["ts"], 100)
        self.assertTrue(snapshot["details_available"])
        self.assertEqual(snapshot["history"]["points"], [])

    def test_reset_transitions_retry_every_return_including_absent_database(self):
        for before, after in ((self.idle, {"generation": "new", "status": "complete"}),
                              (self.idle, {"generation": "old", "status": "clearing"}),
                              (None, self.idle), (self.idle, None)):
            with self.subTest(before=before, after=after):
                # before, reader's state, readback, then a stable second attempt.
                with patch("variational_grid.reset.read_state", side_effect=[before, before, after, after, after, after]):
                    result = read_qqq_snapshot(self.experiment)
                self.assertEqual(result["reset"], after)
                if after and after["status"] == "clearing":
                    self.assertIsNone(result["summary"])
                    self.assertEqual(result["positions"], [])
        (self.experiment.output / "comparison.sqlite3").unlink()
        with patch("variational_grid.reset.read_state", side_effect=[None, None, self.idle, self.idle, self.idle, self.idle]):
            self.assertEqual(read_qqq_snapshot(self.experiment)["reset"], self.idle)

    def test_read_failure_during_reset_retries_but_stable_failure_is_not_hidden(self):
        clearing = {"generation": "old", "status": "clearing"}
        original = fixtures.read_qqq_dashboard
        calls = 0
        def transition(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                save_state(self.experiment, clearing)
                raise sqlite3.OperationalError("database moved")
            return original(*args, **kwargs)
        with patch("variational_grid.qqq_comparison.read_qqq_dashboard", side_effect=transition):
            self.assertIsNone(read_qqq_snapshot(self.experiment)["summary"])
        self.assertEqual(calls, 2)
        with patch("variational_grid.qqq_comparison.read_qqq_dashboard", side_effect=GridError("invalid ledger")):
            with self.assertRaises(GridError):
                read_qqq_snapshot(self.experiment)

    def test_reset_between_account_reads_discards_collected_old_details(self):
        from variational_grid.dashboard import read_db
        opened = 0
        def resetting_read(path):
            nonlocal opened
            opened += 1
            if opened == 3:  # comparison, a, then b
                save_state(self.experiment, {"generation": "old", "status": "clearing"})
            return read_db(path)
        with patch("variational_grid.dashboard.read_db", side_effect=resetting_read):
            result = read_qqq_snapshot(self.experiment)
        self.assertEqual(result["runtime"]["status"], "resetting")
        self.assertIsNone(result["summary"])
        self.assertEqual(result["positions"], [])

    def test_history_through_time_never_moves_past_requested_snapshot(self):
        with closing(sqlite3.connect(self.experiment.output / "comparison.sqlite3")) as db, db:
            row = decode_summary(db.execute("SELECT payload FROM summaries").fetchone()[0])
            row["ts"] = 102
            db.execute("INSERT INTO summaries VALUES (102,?)", (summary_record(row),))
        result = read_qqq_history(self.experiment, "1h", 100)
        self.assertEqual(result["summary_ts"], 100)
        self.assertEqual(result["history"]["points"][-1]["ts"], 100)
        self.assertEqual(result["reset"]["generation"], "old")

    def test_expired_history_budget_stops_scan(self):
        with closing(sqlite3.connect(self.experiment.output / "comparison.sqlite3")) as db:
            db.execute("BEGIN")
            with self.assertRaises(HistoryTimeout):
                read_history(db, 0, 100, ["a", "b"], 2, True, deadline=0)

    def test_http_snapshot_remains_available_during_slow_or_corrupt_history(self):
        server = make_server(self.experiment, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        entered, release = threading.Event(), threading.Event()
        def request(path):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as connection:
                connection.request("GET", path)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
        def slow_history(*args, **kwargs):
            entered.set()
            if not release.wait(4):
                raise AssertionError("Snapshot blocked behind history")
            raise ValueError("private broken state")
        result = []
        history_worker = threading.Thread(target=lambda: result.append(request("/api/qqq-history?range=24h&through=100")))
        try:
            with patch("variational_grid.qqq_history.read_history", side_effect=slow_history):
                history_worker.start()
                self.assertTrue(entered.wait(2))
                status, snapshot = request("/api/qqq-snapshot")
                self.assertEqual(status, 200)
                self.assertEqual(snapshot["summary"]["ts"], 100)
                self.assertTrue(snapshot["details_available"])
                self.assertIn("reset_token", snapshot)
                release.set()
                history_worker.join(3)
            self.assertEqual(result, [(503, {"error": "history_unavailable"})])
            self.assertEqual(request("/api/qqq-history?range=1h&through=100")[0], 200)
        finally:
            release.set()
            if history_worker.ident is not None:
                history_worker.join(5)
            server.shutdown()
            worker.join(5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
