from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest

from test_percentage_volume import quotes
from variational_grid.cli import configuration
from variational_grid.comparison import Cohort, Experiment, Frame
from variational_grid.dashboard import read_dashboard
from variational_grid.engine import Engine
from variational_grid.migration import upgrade_center, upgrade_experiment, upgrade_margin_limit
from variational_grid.models import Config, D, GridError, HOUR
from variational_grid.report import render_report
from variational_grid.store import Store


class UnlimitedEngineTests(unittest.TestCase):
    def test_unlimited_fills_all_levels_with_small_balance_but_never_adds_slots(self):
        for percent, count in [('0.5', 60), ('1', 30), ('2', 15)]:
            for sign in (-1, 1):
                with self.subTest(percent=percent, sign=sign):
                    config = Config(grid_step_percent=percent, max_levels=count, max_margin_fraction=None,
                                    paper_balance_usdc='100', slippage_bps_per_leg='0')
                    store = Store(':memory:', config)
                    try:
                        engine = Engine(config, store)
                        for ts in range(1000, 1000 + count + 3):
                            result = engine.tick(D(4), *quotes(D(4) + sign * D('1.2'), ts), ts)
                        self.assertEqual(result['open_pairs'], count)
                        self.assertEqual(result['actions'], [])
                        self.assertEqual(sorted(lot['level'] for lot in store.lots()), list(range(1, count + 1)))
                        self.assertGreater(D(result['margin_usdc']), D(config.paper_balance_usdc))
                        self.assertGreater(D(result['position_notional_usdc']), D(400))
                        self.assertFalse(result['margin_limit_enabled'])
                        self.assertIsNone(result['margin_limit_usdc'])
                        self.assertIsNone(result['entry_notional_limit_usdc'])
                        self.assertEqual(result['fill_count'], 2 * count)
                        self.assertEqual(D(result['volume_barrels']), 2 * count)
                        self.assertEqual(D(result['grid_range_percent']), 30)
                    finally:
                        store.close()

    def test_unlimited_preserves_entry_drawdown_close_only_and_runtime_drawdown(self):
        config = Config(grid_step_percent='0.5', max_levels=None, max_margin_fraction=None,
                        paper_balance_usdc='100', quantity_barrels='10', slippage_bps_per_leg='100')
        store = Store(':memory:', config)
        try:
            result = Engine(config, store).tick(D(4), *quotes('5.2', 1000, '10'), 1000)
            self.assertEqual(result['skip_reason'], 'entry_drawdown')
            self.assertEqual(result['open_pairs'], 0)
        finally:
            store.close()
        config = replace(config, slippage_bps_per_leg='0')
        store = Store(':memory:', config)
        try:
            engine = Engine(config, store)
            result = engine.tick(D(4), *quotes('5.2', 1000, '10'), 1000, allow_open=False)
            self.assertEqual(result['open_pairs'], 0)
            result = engine.tick(D(4), *quotes('5.2', 1001, '10'), 1001)
            self.assertEqual(result['open_pairs'], 1)
            result = engine.tick(D(4), *quotes('8.2', 1002, '10'), 1002)
            self.assertEqual(result['halted'], 'max_drawdown')
            self.assertEqual(result['open_pairs'], 0)
            self.assertEqual(result['actions'][0]['reason'], 'max_drawdown')
        finally:
            store.close()

    def test_legacy_identity_and_explicit_null_are_distinct(self):
        legacy = Config()
        self.assertEqual(json.loads(legacy.strategy_identity())['max_margin_fraction'], '0.80')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            Store(path, legacy).close()
            with self.assertRaises(GridError):
                Store(path, replace(legacy, max_margin_fraction=None))
        for invalid in ('0', '-1', '1', 'Infinity', 'unlimited'):
            with self.subTest(invalid=invalid), self.assertRaises(GridError):
                replace(legacy, max_margin_fraction=invalid).validate()
        replace(legacy, max_margin_fraction=None).validate()


class UnlimitedMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = self.root / 'base.json'
        self.base.write_text(json.dumps(asdict(Config(grid_step_percent='1', max_levels=30))))
        self.path = self.root / 'experiments.json'
        self.spec = {'base_config': 'base.json', 'center_hours': 72,
                     'output_dir': 'comparison-range30-center3d', 'scenarios': [
                         {'name': f'step-{percent}pct', 'overrides': {'grid_step_percent': percent, 'max_levels': count}}
                         for percent, count in [('0.5', 60), ('1', 30), ('2', 15)]]}
        self.path.write_text(json.dumps(self.spec))

    def test_compare_migrates_once_preserves_old_ledger_base_and_later_customizations(self):
        old = Experiment.load(self.path)
        with Cohort(old):
            pass
        ledger = Path(next(iter(old.scenarios.values())).state_file)
        previous_ledger, previous_base, previous_spec = ledger.read_bytes(), self.base.read_bytes(), self.path.read_bytes()
        backup = upgrade_margin_limit(self.path)
        new = Experiment.load(self.path)
        self.assertEqual(backup.read_bytes(), previous_spec)
        self.assertEqual(ledger.read_bytes(), previous_ledger)
        self.assertEqual(self.base.read_bytes(), previous_base)
        self.assertEqual(new.output.name, old.output.name + '-unlimited-margin')
        self.assertTrue(all(c.max_margin_fraction is None for c in new.scenarios.values()))
        self.assertEqual([c.max_levels for c in new.scenarios.values()], [60, 30, 15])
        self.assertIsNone(upgrade_margin_limit(self.path))
        spec = json.loads(self.path.read_text())
        spec['scenarios'][0]['overrides'].update(max_levels=6, max_margin_fraction='0.5')
        self.path.write_text(json.dumps(spec))
        preserved = self.path.read_bytes()
        self.assertIsNone(upgrade_experiment(self.path))
        self.assertIsNone(upgrade_center(self.path))
        self.assertIsNone(upgrade_margin_limit(self.path))
        self.assertEqual(self.path.read_bytes(), preserved)

    def test_single_first_does_not_hide_saved_comparison_limit(self):
        old = Experiment.load(self.path)
        with Cohort(old):
            pass
        manifest = (old.output / 'experiment.json').read_bytes()
        before = self.base.read_bytes()
        backup = upgrade_margin_limit(self.base, comparison=False)
        self.assertEqual(backup.read_bytes(), before)
        self.assertIsNone(configuration(self.base).max_margin_fraction)
        self.assertTrue(all(c.max_margin_fraction is None for c in Experiment.load(self.path).scenarios.values()))
        self.assertIsNotNone(upgrade_margin_limit(self.path))
        self.assertEqual((old.output / 'experiment.json').read_bytes(), manifest)
        with Cohort(Experiment.load(self.path)) as cohort:
            self.assertIsNone(cohort.latest())
        self.assertIsNone(upgrade_margin_limit(self.base, comparison=False))

    def test_single_uses_saved_identity_if_settings_already_removed_limit(self):
        previous = configuration(self.base)
        Store(previous.state_file, previous).close()
        data = json.loads(self.base.read_text())
        data['max_margin_fraction'] = None
        self.base.write_text(json.dumps(data))
        self.assertIsNotNone(upgrade_margin_limit(self.base, comparison=False))
        self.assertNotEqual(configuration(self.base).state_file, previous.state_file)
        Store(previous.state_file, previous).close()

    def test_collisions_and_conflicting_backups_leave_settings_unchanged(self):
        for comparison, path in [(True, self.path), (False, self.base)]:
            with self.subTest(comparison=comparison):
                before = path.read_bytes()
                current = Experiment.load(path) if comparison else configuration(path)
                old = current.output if comparison else Path(current.state_file)
                target = old.with_name(old.name + '-unlimited-margin') if comparison else old.with_name(old.stem + '-unlimited-margin' + old.suffix)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'occupied')
                with self.assertRaises(GridError):
                    upgrade_margin_limit(path, comparison=comparison)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(target.read_bytes(), b'occupied')
                target.unlink()
                backup = path.with_name(path.stem + '.before-unlimited-margin.json')
                backup.write_bytes(b'different settings')
                with self.assertRaises(GridError):
                    upgrade_margin_limit(path, comparison=comparison)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(backup.read_bytes(), b'different settings')

    def test_examples_dashboard_and_report_expose_null_limits_and_real_amounts(self):
        project = Path(__file__).resolve().parents[1]
        self.base.write_bytes((project / 'config.example.json').read_bytes())
        spec = json.loads((project / 'experiments.example.json').read_text())
        spec.update(base_config='base.json', output_dir='new-comparison')
        self.path.write_text(json.dumps(spec))
        self.assertIsNone(upgrade_margin_limit(self.path))
        experiment = Experiment.load(self.path)
        ts = 1735689600
        with Cohort(experiment) as cohort:
            cohort.ingest(Frame(ts, ts // HOUR * HOUR, D(4), {'1': quotes('5.2', ts)}))
            data = read_dashboard(experiment)
            for row in data['summary']['scenarios']:
                self.assertFalse(row['margin_limit_enabled'])
                self.assertIsNone(row['entry_notional_limit_usdc'])
                self.assertGreater(D(row['position_notional_usdc']), 0)
                self.assertEqual(D(row['position_notional_usdc']), D(row['margin_usdc']) * 100)
            html = render_report(data['summary'], [], {'status': 'running'})
            self.assertEqual(html.count('金额不限'), 3)
