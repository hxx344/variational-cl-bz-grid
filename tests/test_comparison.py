from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from variational_grid.comparison import Cohort, Experiment, Frame, MarketFeed
from variational_grid.models import Config, D, GridError, HOUR, Quote
from variational_grid.report import chart, render_report


BASE = 1735689600


def frame(spread="7.22", step=0):
    ts = BASE + 10 * step
    quotes = []
    for symbol, mark in (("CL", D(95)), ("BZ", D(95) + D(spread))):
        quotes.append(Quote(symbol, mark - D("0.02"), mark + D("0.02"), mark, D(1), ts))
    return Frame(ts, int(ts) // HOUR * HOUR, D(7), {"1": tuple(quotes)})


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / "base.json"
        self.base.write_text(json.dumps(asdict(Config())), encoding="utf-8")
        self.path = self.root / "experiments.json"
        self.spec = {"base_config": "base.json", "output_dir": "comparison", "scenarios": [
            {"name": f"step-{step}", "overrides": {"grid_step_usdc_per_barrel": step}} for step in ("0.15", "0.20", "0.25")]}
        self.path.write_text(json.dumps(self.spec), encoding="utf-8")
        self.experiment = Experiment.load(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_selected_intervals_have_isolated_positions_and_same_time(self):
        with Cohort(self.experiment) as cohort:
            result = cohort.ingest(frame())
            self.assertEqual([r["open_pairs"] for r in result["scenarios"]], [1, 1, 0])
            self.assertEqual(len({r["time_utc"] for r in result["scenarios"]}), 1)
            self.assertEqual(result["sample_count"], 1)
            self.assertEqual([r["initial_balance_usdc"] for r in result["scenarios"]], ["1000"] * 3)
            self.assertEqual(len({s.db for s in cohort.stores.values()}), 3)
            result = cohort.ingest(frame("6.7", 1))
            self.assertEqual([r["closed_pairs"] for r in result["scenarios"]], [1, 1, 0])
            self.assertEqual([r["winning_pairs"] for r in result["scenarios"]], [1, 1, 0])

    def test_shared_frame_is_replayed_only_for_unfinished_scenarios(self):
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with Cohort(self.experiment) as cohort:
                with patch.object(cohort.engines["step-0.20"], "tick", side_effect=RuntimeError("injected")):
                    cohort.ingest(frame())
        with Cohort(self.experiment) as cohort:
            result = cohort.latest()
            self.assertEqual(result["sample_count"], 1)
            self.assertEqual([r["opened_pairs"] for r in result["scenarios"]], [1, 1, 0])
            counts = [s.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] for s in cohort.stores.values()]
            self.assertEqual(counts, [2, 2, 0])
            self.assertEqual(cohort.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 1)

    def test_crash_after_all_legs_before_publishing_recovers(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with Cohort(self.experiment) as cohort:
                cohort.db.execute("CREATE TRIGGER fail_summary BEFORE INSERT ON summaries BEGIN SELECT RAISE(ABORT, 'injected'); END")
                try:
                    cohort.ingest(frame())
                finally:
                    cohort.db.execute("DROP TRIGGER fail_summary")
                    cohort.db.commit()
        with Cohort(self.experiment) as cohort:
            self.assertEqual(cohort.latest()["sample_count"], 1)
            self.assertEqual(cohort.stores["step-0.15"].db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 2)

    def test_restart_keeps_lifetime_metrics(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
            before = cohort.ingest(frame("6.7", 1))
        with Cohort(self.experiment) as cohort:
            self.assertEqual(cohort.latest(), before)
            after = cohort.ingest(frame("7", 2))
            self.assertEqual(after["sample_count"], 3)
            self.assertGreaterEqual(D(after["scenarios"][0]["max_drawdown_fraction"]), D(before["scenarios"][0]["max_drawdown_fraction"]))

    def test_invalid_quantity_frame_stops_every_scenario_before_journaling(self):
        bad = frame()
        bad.quotes["1"] = (bad.quotes["1"][0], replace(bad.quotes["1"][1], ts=BASE-100))
        with Cohort(self.experiment) as cohort:
            with self.assertRaises(GridError):
                cohort.ingest(bad)
            self.assertEqual(cohort.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 0)
            self.assertTrue(all(not store.lots() for store in cohort.stores.values()))

    def test_out_of_order_frame_does_not_duplicate_trades(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
            with self.assertRaises(GridError):
                cohort.ingest(frame())
            self.assertEqual(cohort.latest()["sample_count"], 1)

    def test_changed_parameters_require_new_cohort(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
        self.spec["scenarios"][0]["overrides"]["quantity_barrels"] = "2"
        self.path.write_text(json.dumps(self.spec), encoding="utf-8")
        with self.assertRaises(GridError):
            with Cohort(Experiment.load(self.path)):
                self.fail("Mismatched cohort accepted")

    def test_unmanaged_output_files_are_preserved(self):
        self.experiment.output.mkdir()
        original = self.experiment.output / "my-file.txt"
        original.write_text("keep")
        with self.assertRaises(GridError):
            with Cohort(self.experiment):
                pass
        self.assertEqual(original.read_text(), "keep")

    def test_unsafe_names_duplicates_and_override_paths_rejected(self):
        for scenario in ({"name": "../escape", "overrides": {}}, {"name": "bad", "overrides": {"session_file": "outside"}},
                         {"name": "step-0.20", "overrides": {}}, {"name": "bad", "overrides": {"mode": "live"}}):
            self.spec["scenarios"][0] = scenario
            self.path.write_text(json.dumps(self.spec), encoding="utf-8")
            with self.assertRaises(GridError):
                Experiment.load(self.path)

    def test_report_survives_pause_and_has_units_and_escaping(self):
        with Cohort(self.experiment) as cohort:
            result = cohort.ingest(frame())
            cohort.set_runtime("paused", "<script>untrusted</script>")
            text = (self.experiment.output / "public/index.html").read_text(encoding="utf-8")
            self.assertIn("0.15", text)
            self.assertIn("USDC/桶", text)
            self.assertIn("未计实际资金费", text)
            self.assertIn("&lt;script&gt;untrusted&lt;/script&gt;", text)
            self.assertNotIn("<script>untrusted", text)
            self.assertIn("行情已过期", text)
            self.assertEqual(cohort.latest(), result)

    def test_chart_preserves_time_gaps(self):
        with Cohort(self.experiment) as cohort:
            a = cohort.ingest(frame())
            b = cohort.ingest(frame("7.2", 100))
        svg = chart("step-0.15", [a,b], -1, 1, 10)
        self.assertIn('d="M', svg)
        self.assertIn(' M', svg)

    def test_feed_fetches_each_quantity_only_once_and_caches_hourly_history(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
            def candles(self, symbol, hour):
                self.calls.append(("candles", symbol))
                return [{"unix_time_ms": t*1000, "close": "95" if symbol=="CL" else "102"} for t in range(hour-168*HOUR,hour,HOUR)]
            def market(self, symbol):
                return True, False
            def quote(self, symbol, qty):
                self.calls.append(("quote", symbol, str(qty)))
                return frame().quotes["1"][0 if symbol=="CL" else 1]
        client = FakeClient()
        feed = MarketFeed(self.experiment, client)
        with patch("variational_grid.comparison.time.time", return_value=BASE):
            feed.next()
            feed.next()
        self.assertEqual(sum(c[0]=="candles" for c in client.calls), 2)
        self.assertEqual(sum(c[0]=="quote" for c in client.calls), 4)  # Two symbols × two polls, not × three scenarios.


if __name__ == "__main__":
    unittest.main()
