from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from variational_grid.engine import Engine
from variational_grid.migration import upgrade_experiment
from variational_grid.models import Config, D, GridError, Quote
from variational_grid.comparison import Cohort, Experiment, Frame
from variational_grid.dashboard import read_dashboard
from variational_grid.store import Store, fill_totals


def quotes(spread, ts, qty="1"):
    return tuple(Quote(symbol, mark, mark, mark, D(qty), ts)
                 for symbol, mark in (("CL", D(95)), ("BZ", D(95) + D(spread))))


class PercentageVolumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def engine(self, percent="1", **kwargs):
        config = Config(grid_step_percent=percent, slippage_bps_per_leg="0", **kwargs)
        store = Store(self.root / f"{percent}.sqlite3", config)
        self.addCleanup(store.close)
        return Engine(config, store)

    def tick(self, engine, spread, ts, center="4", **kwargs):
        return engine.tick(D(center), *quotes(spread, ts, engine.config.quantity_barrels), ts, **kwargs)

    def test_percentage_boundaries_and_independent_levels(self):
        for percent, step in (("0.5", "0.02"), ("1", "0.04"), ("2", "0.08")):
            with self.subTest(percent=percent):
                engine = self.engine(percent)
                before = self.tick(engine, D(4) + D(step) - D("0.000001"), 1000)
                self.assertEqual(before["open_pairs"], 0)
                self.assertEqual(before["fill_count"], 0)
                after = self.tick(engine, D(4) + D(step), 1010)
                self.assertEqual(after["open_pairs"], 1)
                self.assertEqual(D(after["grid_step"]), D(step))
                self.assertEqual(after["grid_step_percent"], percent)
                self.assertEqual(after["actions"][0]["direction"], -1)

    def test_take_profit_keeps_entry_target_when_center_doubles(self):
        engine = self.engine()
        self.tick(engine, "4.04", 1000)
        result = self.tick(engine, "4.00", 1010, center="8")
        self.assertEqual(result["open_pairs"], 0)
        self.assertEqual(result["actions"][0]["reason"], "take_profit")
        self.assertEqual(D(result["realized_pnl_usdc"]), D("0.04"))
        self.assertEqual(D(result["grid_step"]), D("0.08"))

    def test_center_halving_does_not_lower_existing_target(self):
        engine = self.engine()
        self.tick(engine, "4.04", 1000)
        result = self.tick(engine, "4.02", 1010, center="2", allow_open=False)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["open_pairs"], 1)

    def test_negative_and_zero_center_allow_safe_existing_exits(self):
        engine = self.engine()
        self.assertEqual(self.tick(engine, "-4.04", 1000, center="-4")["open_pairs"], 1)
        result = self.tick(engine, "-4.00", 1010, center="0")
        self.assertEqual(result["open_pairs"], 0)
        self.assertEqual(result["actions"][0]["reason"], "take_profit")
        result = self.tick(engine, "4", 1020, center="0")
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["skip_reason"], "zero_center")

    def test_volume_is_gross_execution_notional_not_quotes_or_fees(self):
        engine = self.engine(quantity_barrels="1.25", fee_bps_per_leg="1")
        opened = self.tick(engine, "4.04", 1000)
        self.assertEqual(D(opened["volume_barrels"]), D("2.5"))
        self.assertEqual(D(opened["turnover_usdc"]), D("1.25") * D("194.04"))
        held = self.tick(engine, "4.04", 1010)
        self.assertEqual(held["turnover_usdc"], opened["turnover_usdc"])
        closed = self.tick(engine, "3.9", 1020)
        self.assertEqual(closed["open_pairs"], 0)
        self.assertEqual(D(closed["volume_barrels"]), D(5))
        self.assertEqual(closed["fill_count"], 4)
        self.assertEqual(D(closed["turnover_usdc"]), D("1.25") * (D("194.04") + D("193.9")))
        self.assertEqual(engine.store.volume(), fill_totals(engine.store.db))

    def test_volume_rolls_back_with_failed_second_leg(self):
        engine = self.engine()
        engine.store.db.execute("CREATE TRIGGER fail_bz BEFORE INSERT ON fills WHEN NEW.symbol='BZ' BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.tick(engine, "4.04", 1000)
        self.assertEqual(engine.store.volume(), {"volume_barrels": "0", "turnover_usdc": "0", "fill_count": 0})
        self.assertEqual(engine.store.lots(), [])

    def test_legacy_identity_and_one_time_volume_backfill(self):
        config = Config()
        legacy = asdict(config)
        for key in ("grid_step_percent", "poll_seconds", "max_quote_age_seconds", "max_pair_skew_seconds", "session_file", "state_file"):
            legacy.pop(key)
        self.assertEqual(config.strategy_identity(), json.dumps(legacy, sort_keys=True, separators=(",", ":")))
        path = self.root / "legacy.sqlite3"
        store = Store(path, config)
        engine = Engine(config, store)
        self.tick(engine, "4.2", 1000)
        expected = store.volume()
        with store.transaction():
            store.db.execute("DELETE FROM meta WHERE key IN ('volume_barrels','turnover_usdc','fill_count')")
        store.close()
        for i in range(2):
            store = Store(path, config)
            try:
                self.assertEqual(store.volume(), expected)
                after = self.tick(Engine(config, store), "4.2", 1010 + 10*i)
                self.assertEqual(after["fill_count"], 2)
            finally:
                store.close()
        with self.assertRaises(GridError):
            Store(path, replace(config, grid_step_percent="1"))

    def test_invalid_percentage_is_rejected(self):
        for value in ("0", "-1", "100.01", "NaN", "Infinity", "1%"):
            with self.subTest(value=value), self.assertRaises(GridError):
                Config(grid_step_percent=value).validate()

    def test_default_percentage_cohort_publishes_correct_units_and_volumes(self):
        (self.root / "base.json").write_text(json.dumps(asdict(Config(slippage_bps_per_leg="0"))))
        spec = json.loads((Path(__file__).resolve().parents[1] / "experiments.example.json").read_text())
        spec.update(base_config="base.json", output_dir="new")
        path = self.root / "experiments.json"
        path.write_text(json.dumps(spec))
        experiment = Experiment.load(path)
        ts = 1735689600
        with Cohort(experiment) as cohort:
            rows = cohort.ingest(Frame(ts, ts, D(4), {"1": quotes("4.06", ts)}))["scenarios"]
            self.assertEqual([r["grid_step_percent"] for r in rows], ["0.5", "1", "2"])
            self.assertEqual([r["volume_barrels"] for r in rows], ["2", "2", "0"])
            self.assertEqual([r["fill_count"] for r in rows], [2, 2, 0])
            dashboard = read_dashboard(experiment)
            self.assertEqual([D(p["target_pnl_usdc"]) for p in dashboard["positions"]], [D("0.02"), D("0.04")])
            report = (experiment.output / "public/index.html").read_text(encoding="utf-8")
            self.assertIn("0.5%", report)
            self.assertIn("累计成交额", report)
        with Cohort(experiment) as cohort:
            self.assertEqual([r["fill_count"] for r in cohort.latest()["scenarios"]], [2, 2, 0])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "base.json").write_text(json.dumps(asdict(Config(quantity_barrels="2"))))
        self.path = self.root / "experiments.json"
        self.spec = {"base_config": "base.json", "output_dir": "comparison-015-020-025", "scenarios": [
            {"name": f"step-{step}", "overrides": {"grid_step_usdc_per_barrel": step, "max_levels": 3}}
            for step in ("0.15", "0.20", "0.25")]}
        self.original = json.dumps(self.spec).encode()
        self.path.write_bytes(self.original)
        self.old = self.root / self.spec["output_dir"]
        self.old.mkdir()
        (self.old / "preserve-me").write_bytes(b"old ledger")

    def test_migration_preserves_old_data_costs_quantity_and_is_idempotent(self):
        backup = upgrade_experiment(self.path)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual((self.old / "preserve-me").read_bytes(), b"old ledger")
        experiment = Experiment.load(self.path)
        self.assertEqual(experiment.output.name, "comparison-pct-05-1-2")
        self.assertEqual([c.grid_step_percent for c in experiment.scenarios.values()], ["0.5", "1", "2"])
        self.assertTrue(all(c.quantity_barrels == "2" and c.max_levels == 3 for c in experiment.scenarios.values()))
        after = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.assertIsNone(upgrade_experiment(self.path))
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), after)
        self.assertFalse(list(self.root.glob(".experiment-upgrade-*")))

    def test_migration_conflict_leaves_original_config_and_data_untouched(self):
        dest = self.root / "comparison-pct-05-1-2"
        dest.mkdir()
        (dest / "keep").write_bytes(b"existing experiment")
        with self.assertRaises(GridError):
            upgrade_experiment(self.path)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual((dest / "keep").read_bytes(), b"existing experiment")

    def test_custom_experiments_are_not_silently_changed(self):
        self.spec["scenarios"][0]["overrides"]["grid_step_usdc_per_barrel"] = "0.3"
        self.path.write_text(json.dumps(self.spec))
        original = self.path.read_bytes()
        self.assertIsNone(upgrade_experiment(self.path))
        self.assertEqual(self.path.read_bytes(), original)
