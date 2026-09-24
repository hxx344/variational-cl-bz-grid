from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from variational_grid.models import Config, GridError
from variational_grid.qqq_comparison import QQQExperiment, QQQCohort, QQQMarketFeed
from variational_grid.qqq_hedge import QQQSettings
from variational_grid.qqq_market import VarSwapClient
from variational_grid.qqq_migration import upgrade_qqq_defaults, VERSION
from variational_grid.qqq_scalper import CURRENT_MODEL, ScalperSettings
from test_qqq import market
from test_qqq_cache import PublicSource
from test_qqq_market import NOW


def legacy_spec(base="base.json", output="old"):
    return {"kind": "qqq_hedge", "base_config": base, "output_dir": output, "strategy": asdict(QQQSettings()),
            "scenarios": [{"name": f"grid-{step}-hedge-{band}", "grid_step_percent": step, "hedge_tolerance_percent": band}
                          for step in ("0.05", "0.1", "0.2") for band in ("0", "2", "5")]}


def three_spec(base="base.json", output="old-three"):
    return {"kind": "qqq_hedge", "base_config": base, "output_dir": output,
            "strategy": asdict(replace(QQQSettings(), var_slippage_bps="0")),
            "pricing": {"mode": "shared_indicative_v1", "half_spread_percent": "0.0015"},
            "scenarios": [{"name": f"grid-{step}-hedge-3000usd", "grid_step_percent": step, "hedge_threshold_usdc": "3000"}
                          for step in ("0.05", "0.1", "0.2")]}


def scalper_spec(base="base.json", output="old-scalper-v1"):
    data = three_spec(base, output)
    data["scalper"] = asdict(ScalperSettings())
    for row in data["scenarios"]:
        row["take_profit_percent"] = row["grid_step_percent"]
    return data


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "base.json").write_text(json.dumps(asdict(Config())))
        self.path = self.root / "qqq.json"
        self.path.write_text(json.dumps(legacy_spec()))

    def test_default_upgrade_preserves_old_ledger_and_is_idempotent(self):
        old = QQQExperiment.load(self.path)
        with QQQCohort(old):
            pass
        manifest = (old.output / "experiment.json").read_bytes()
        original = self.path.read_bytes()
        backup = upgrade_qqq_defaults(self.path)
        new = QQQExperiment.load(self.path)
        self.assertEqual(len(new.scenarios), 3)
        self.assertTrue(all(c.hedge_threshold_usdc == "3000" for c in new.scenarios.values()))
        self.assertEqual(new.settings.var_slippage_bps, "0")
        self.assertEqual(new.pricing.half_spread_percent, "0.0015")
        self.assertEqual(new.scalper.wait_seconds, 450)
        self.assertEqual(new.scalper.model, CURRENT_MODEL)
        self.assertTrue(all(c.take_profit_percent == c.grid_step_percent for c in new.scenarios.values()))
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual((old.output / "experiment.json").read_bytes(), manifest)
        self.assertEqual(new.previous_output, old.output)
        updated = self.path.read_bytes()
        self.assertIsNone(upgrade_qqq_defaults(self.path))
        self.assertEqual(self.path.read_bytes(), updated)
        with QQQCohort(new):
            pass
        self.assertNotEqual(old.identity(), new.identity())

    def test_custom_economics_or_grid_are_not_overwritten(self):
        for field, value in (("grid_count", 20), ("var_slippage_bps", "2")):
            data = legacy_spec()
            data["strategy"][field] = value
            self.path.write_text(json.dumps(data))
            original = self.path.read_bytes()
            self.assertIsNone(upgrade_qqq_defaults(self.path))
            self.assertEqual(self.path.read_bytes(), original)

    def test_existing_target_or_conflicting_backup_is_preserved(self):
        target = self.root / ("old" + VERSION)
        target.mkdir()
        sentinel = target / "keep"
        sentinel.write_bytes(b"keep")
        original = self.path.read_bytes()
        with self.assertRaises(GridError):
            upgrade_qqq_defaults(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        backup = self.path.with_name("qqq.before-scalper-v2.json")
        backup.write_bytes(b"other config")
        with self.assertRaisesRegex(GridError, "backup"):
            upgrade_qqq_defaults(self.path)
        self.assertEqual(backup.read_bytes(), b"other config")

    def test_current_three_ladder_gets_new_identity_without_overwriting_history(self):
        self.path.write_text(json.dumps(three_spec()))
        old = QQQExperiment.load(self.path)
        with QQQCohort(old):
            pass
        manifest = (old.output / "experiment.json").read_bytes()
        backup = upgrade_qqq_defaults(self.path)
        new = QQQExperiment.load(self.path)
        self.assertIsNotNone(backup)
        self.assertEqual(new.previous_output, old.output)
        self.assertEqual(new.scalper.wait_seconds, 450)
        self.assertNotEqual(old.identity(), new.identity())
        self.assertEqual((old.output / "experiment.json").read_bytes(), manifest)
        with QQQCohort(new), QQQCohort(old):
            pass

    def test_unused_intermediate_three_run_preserves_original_quote_inheritance(self):
        data = three_spec()
        data["previous_output_dir"] = "original-nine"
        self.path.write_text(json.dumps(data))
        upgrade_qqq_defaults(self.path)
        self.assertEqual(QQQExperiment.load(self.path).previous_output, (self.root / "original-nine").resolve())

    def test_custom_current_three_threshold_is_preserved(self):
        data = three_spec()
        data["scenarios"][0]["hedge_threshold_usdc"] = "2000"
        self.path.write_text(json.dumps(data))
        original = self.path.read_bytes()
        self.assertIsNone(upgrade_qqq_defaults(self.path))
        self.assertEqual(self.path.read_bytes(), original)

    def test_v1_upgrade_keeps_old_accounts_and_backup_with_distinct_new_identity(self):
        self.path.write_text(json.dumps(scalper_spec()))
        old = QQQExperiment.load(self.path)
        with QQQCohort(old):
            pass
        history = {p.relative_to(old.output): p.read_bytes() for p in old.output.rglob("*") if p.is_file()}
        original = self.path.read_bytes()
        prior_backup = self.path.with_name("qqq.before-scalper-v1.json")
        prior_backup.write_bytes(b"earlier ladder configuration")
        backup = upgrade_qqq_defaults(self.path)
        new = QQQExperiment.load(self.path)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(prior_backup.read_bytes(), b"earlier ladder configuration")
        self.assertEqual(new.scalper.model, CURRENT_MODEL)
        self.assertFalse(new.scalper.entry_distance_enabled)
        self.assertEqual(new.previous_output, old.output)
        self.assertNotEqual(new.identity(), old.identity())
        self.assertEqual(history, {p.relative_to(old.output): p.read_bytes() for p in old.output.rglob("*") if p.is_file()})
        with QQQCohort(new) as cohort, QQQCohort(old):
            self.assertIsNone(cohort.latest())
        updated = self.path.read_bytes()
        self.assertIsNone(upgrade_qqq_defaults(self.path))
        self.assertEqual(updated, self.path.read_bytes())

    def test_custom_scalper_timing_tp_and_settings_are_preserved(self):
        for section, key, value in (("scalper", "wait_seconds", 400), ("strategy", "grid_count", 20),
                                    ("scalper", "reprice_after_seconds", 15), ("pricing", "half_spread_percent", "0.002")):
            data = scalper_spec()
            data[section][key] = value
            self.path.write_text(json.dumps(data))
            original = self.path.read_bytes()
            self.assertIsNone(upgrade_qqq_defaults(self.path))
            self.assertEqual(self.path.read_bytes(), original)
        data = scalper_spec()
        data["scenarios"][0]["take_profit_percent"] = "0.03"
        self.path.write_text(json.dumps(data))
        original = self.path.read_bytes()
        self.assertIsNone(upgrade_qqq_defaults(self.path))
        self.assertEqual(self.path.read_bytes(), original)

    def test_custom_price_model_is_preserved_while_cache_timing_can_migrate(self):
        for policy in ({"mode": "exact_quantity"}, {"half_spread_percent": "0.01"}):
            data = legacy_spec()
            data["pricing"] = policy
            self.path.write_text(json.dumps(data))
            original = self.path.read_bytes()
            self.assertIsNone(upgrade_qqq_defaults(self.path))
            self.assertEqual(self.path.read_bytes(), original)
        data["pricing"] = {"refresh_after_seconds": 4, "max_age_seconds": 90}
        self.path.write_text(json.dumps(data))
        self.assertIsNotNone(upgrade_qqq_defaults(self.path))
        new = QQQExperiment.load(self.path)
        self.assertEqual((new.pricing.refresh_after_seconds, new.pricing.max_age_seconds), (4, 90))

    def test_new_output_inherits_cooldown_before_any_http_without_precreating_directory(self):
        self.path.write_text(json.dumps(scalper_spec()))
        old = QQQExperiment.load(self.path)
        now = [NOW]
        source = PublicSource(now)
        var = VarSwapClient(opener=source, clock=lambda: now[0], max_age_seconds=60)
        q = SimpleNamespace(snapshot=lambda: market(now[0]))
        feed = QQQMarketFeed(old, q, var)
        with QQQCohort(old) as cohort:
            with patch("variational_grid.qqq_comparison.time.time", side_effect=lambda: now[0]):
                cohort.ingest(feed.next(cohort))
                now[0] += 4
                source.failure = 429
                cohort.ingest(feed.next(cohort))
        upgrade_qqq_defaults(self.path)
        new = QQQExperiment.load(self.path)
        replacement = VarSwapClient(opener=source, clock=lambda: now[0], max_age_seconds=60)
        feed = QQQMarketFeed(new, q, replacement)
        self.assertFalse(new.output.exists())
        self.assertGreater(replacement.transport.retry_at, now[0])
        with QQQCohort(new) as cohort:
            with patch("variational_grid.qqq_comparison.time.time", side_effect=lambda: now[0]):
                observation = feed.next(cohort)
            self.assertTrue(observation.market["allow_entries"])
            self.assertEqual(observation.market["var"]["ts"], NOW)
            self.assertEqual(len(source.posts), 2)
            self.assertTrue((new.output / "quote-cache.json").exists())


if __name__ == "__main__":
    unittest.main()
