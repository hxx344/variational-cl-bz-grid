from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest

import test_unlimited_margin
from test_percentage_volume import quotes
from variational_grid.cli import configuration
from variational_grid.comparison import Cohort, Experiment
from variational_grid.engine import Engine
from variational_grid.migration import UNBOUNDED_VERSION, upgrade_center, upgrade_experiment, upgrade_margin_limit, upgrade_unbounded_grid
from variational_grid.models import Config, D, GridError
from variational_grid.store import Store


class UnboundedEngineTests(unittest.TestCase):
    def test_all_steps_extend_past_old_range_and_past_100_positions(self):
        for percent in ('0.5', '1', '2'):
            for sign in (-1, 1):
                with self.subTest(percent=percent, sign=sign):
                    config = Config(grid_step_percent=percent, max_levels=None, max_margin_fraction=None,
                                    paper_leverage='100', paper_balance_usdc='100', slippage_bps_per_leg='0')
                    store = Store(':memory:', config)
                    try:
                        engine = Engine(config, store)
                        for ts in range(1000, 1121):
                            result = engine.tick(D(4), *quotes(D(4) + sign * 12, ts), ts)
                        self.assertEqual(result['open_pairs'], 121)
                        self.assertEqual(result['actions'][0]['level'], 121)
                        self.assertEqual(result['actions'][0]['direction'], -sign)
                        self.assertEqual(len({(l['direction'],l['level']) for l in store.lots()}), 121)
                        self.assertFalse(result['grid_limit_enabled'])
                        self.assertIsNone(result['grid_upper'])
                        self.assertIsNone(result['grid_range_percent'])
                        self.assertIsNone(result['entry_notional_limit_usdc'])
                    finally:
                        store.close()

    def test_extreme_theoretical_depth_sparse_slots_rearm_zero_and_restart(self):
        config = Config(grid_step_percent='0.5', max_levels=None, max_margin_fraction=None,
                        paper_leverage='100', slippage_bps_per_leg='0')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite3'
            store = Store(path, config)
            engine = Engine(config, store)
            try:
                center = D('1e-1000')  # Enumerating the theoretical depth is impossible.
                with store.transaction():
                    engine.open(-1, 1, center, *quotes('4',1000),1000)
                    engine.open(-1, 3, center, *quotes('4',1000),1000)
                    store.set('blocked', json.dumps([[-1, 2]]))
                result = engine.tick(center,*quotes('4',1001),1001)
                self.assertEqual(result['actions'][0]['level'],4)
            finally:
                store.close()
            store = Store(path, config)
            try:
                engine = Engine(config, store)
                result = engine.tick(center,*quotes('4',1002),1002)
                self.assertEqual(result['actions'][0]['level'],5)
                # Zero center clears rearm thresholds, but must never open another lot.
                result = engine.tick(D(0),*quotes('4',1003),1003)
                self.assertEqual(result['skip_reason'],'zero_center')
                self.assertEqual(result['actions'],[])
                result = engine.tick(center,*quotes('4',1004),1004)
                self.assertEqual(result['actions'][0]['level'],2)
            finally:
                store.close()

    def test_leverage_changes_margin_only_and_keeps_exit_and_close_only_controls(self):
        outcomes = []
        for leverage in ('5','100'):
            config = Config(grid_step_percent='0.5', max_levels=None, max_margin_fraction=None,
                            paper_leverage=leverage, fee_bps_per_leg='1')
            store = Store(':memory:',config)
            try:
                engine = Engine(config,store)
                closed_only = engine.tick(D(4),*quotes('5.2',1000),1000,allow_open=False)
                self.assertEqual(closed_only['open_pairs'],0)
                opening = engine.tick(D(4),*quotes('5.2',1001),1001)
                closing = engine.tick(D(4),*quotes('4',1002),1002)
                self.assertEqual(closing['actions'][0]['reason'],'take_profit')
                outcomes.append((opening,closing))
            finally:
                store.close()
        self.assertEqual(D(outcomes[0][0]['margin_usdc']),D(outcomes[1][0]['margin_usdc'])*20)
        for phase in (0,1):
            for field in ('open_pairs','position_notional_usdc','equity_usdc','fees_usdc','volume_barrels','turnover_usdc','total_pnl_usdc'):
                self.assertEqual(D(outcomes[0][phase][field]),D(outcomes[1][phase][field]))

    def test_legacy_identity_remains_finite_and_unbounded_identity_is_distinct(self):
        legacy = Config()
        self.assertEqual(json.loads(legacy.strategy_identity())['max_levels'],8)
        self.assertEqual(legacy.paper_leverage,'5')
        self.assertNotEqual(legacy.strategy_identity(),replace(legacy,max_levels=None).strategy_identity())
        for bad in (0,-1,True,'null',101):
            with self.subTest(bad=bad),self.assertRaises(GridError):
                replace(legacy,max_levels=bad).validate()
        replace(legacy,max_levels=None,paper_leverage='100').validate()


class UnboundedMigrationTests(unittest.TestCase):
    setUp = test_unlimited_margin.UnlimitedMigrationTests.setUp

    def test_new_policy_preserves_ledgers_and_repeat_installs_preserve_custom_changes(self):
        old = Experiment.load(self.path)
        with Cohort(old):
            pass
        ledger = Path(next(iter(old.scenarios.values())).state_file)
        previous = ledger.read_bytes()
        base = self.base.read_bytes()
        backup = upgrade_unbounded_grid(self.path)
        current = Experiment.load(self.path)
        self.assertEqual(json.loads(backup.read_text()), self.spec)
        self.assertEqual(current.output.name,old.output.name+UNBOUNDED_VERSION)
        self.assertEqual(ledger.read_bytes(),previous)
        self.assertEqual(self.base.read_bytes(),base)
        self.assertTrue(all(c.max_levels is None and c.max_margin_fraction is None and c.paper_leverage=='100' for c in current.scenarios.values()))
        spec = json.loads(self.path.read_text())
        spec['scenarios'][0]['overrides'].update(max_levels=6,paper_leverage='25',max_margin_fraction='0.5')
        self.path.write_text(json.dumps(spec))
        previous = self.path.read_bytes()
        for migrate in (upgrade_experiment,upgrade_center,upgrade_margin_limit,upgrade_unbounded_grid):
            self.assertIsNone(migrate(self.path))
        self.assertEqual(self.path.read_bytes(),previous)

    def test_single_first_still_versions_saved_inherited_comparison_identity(self):
        for row in self.spec['scenarios']:
            row['overrides'].pop('max_levels')
        self.path.write_text(json.dumps(self.spec))
        old = Experiment.load(self.path)
        with Cohort(old):
            pass
        manifest = (old.output/'experiment.json').read_bytes()
        backup = upgrade_unbounded_grid(self.base,comparison=False)
        self.assertEqual(configuration(self.base).paper_leverage,'100')
        self.assertIsNone(configuration(self.base).max_levels)
        self.assertIsNotNone(backup)
        self.assertIsNotNone(upgrade_unbounded_grid(self.path))
        self.assertEqual((old.output/'experiment.json').read_bytes(),manifest)
        with Cohort(Experiment.load(self.path)) as cohort:
            self.assertIsNone(cohort.latest())

    def test_saved_single_identity_and_collision_fail_closed(self):
        old = configuration(self.base)
        Store(old.state_file,old).close()
        data = json.loads(self.base.read_text())
        data.update(max_levels=None,max_margin_fraction=None,paper_leverage='100')
        self.base.write_text(json.dumps(data))
        self.assertIsNotNone(upgrade_unbounded_grid(self.base,comparison=False))
        Store(old.state_file,old).close()
        target = Experiment.load(self.path).output.with_name(Experiment.load(self.path).output.name+UNBOUNDED_VERSION)
        target.mkdir()
        before = self.path.read_bytes()
        with self.assertRaises(GridError):
            upgrade_unbounded_grid(self.path)
        self.assertEqual(self.path.read_bytes(),before)
        target.rmdir()
        backup = self.path.with_name('experiments.before'+UNBOUNDED_VERSION+'.json')
        backup.write_bytes(b'different settings')
        with self.assertRaises(GridError):
            upgrade_unbounded_grid(self.path)
        self.assertEqual(self.path.read_bytes(),before)

    def test_old_range_migration_accepts_unbounded_configs_without_arithmetic_error(self):
        self.spec['output_dir']='custom-unbounded'
        for row in self.spec['scenarios']:
            row['overrides']['max_levels']=None
        self.path.write_text(json.dumps(self.spec))
        self.assertIsNone(upgrade_experiment(self.path))
