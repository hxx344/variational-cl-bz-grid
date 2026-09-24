from contextlib import closing
from contextlib import redirect_stdout
import argparse
from dataclasses import asdict, replace
import http.client
import io
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

import test_dashboard
from test_dashboard import frame
from variational_grid.cli import configuration
from variational_grid.comparison import Cohort, Experiment, run_comparison
from variational_grid.dashboard import make_server, read_dashboard
from variational_grid.migration import upgrade_center
from variational_grid.models import Config, D, GridError, HOUR, rolling_center
from variational_grid.reset import process_reset, read_state, request_reset
from variational_grid.store import Store


class CenterResetTests(unittest.TestCase):
    setUp = test_dashboard.DashboardTests.setUp

    def test_72_hour_center_ignores_older_extreme_prices_and_open_candle(self):
        end = 1735689600
        cl = [{"unix_time_ms": t*1000, "close": "95"} for t in range(end-168*HOUR, end+HOUR, HOUR)]
        bz = [{**r, "close": "99" if end-72*HOUR <= r['unix_time_ms']/1000 < end else "999999"} for r in cl]
        self.assertEqual(rolling_center(cl, bz, end), D(4))
        with self.assertRaises(GridError):
            rolling_center(cl[:-2], bz, end)

    def test_legacy_identity_does_not_accept_new_center(self):
        legacy = Config(center_hours=168)
        self.assertNotIn('center_hours', json.loads(legacy.strategy_identity()))
        path = self.root / 'legacy.sqlite3'
        Store(path, legacy).close()
        with self.assertRaises(GridError):
            Store(path, replace(legacy, center_hours=72))
        base = asdict(legacy)
        base.pop('center_hours')
        (self.root / 'base.json').write_text(json.dumps(base))
        self.assertEqual(configuration(self.root / 'base.json').center_hours, 168)

    def test_comparison_migration_preserves_settings_and_ledgers_and_repeats_noop(self):
        base = asdict(Config(center_hours=168, quantity_barrels='2', paper_balance_usdc='1700'))
        base.pop('center_hours')
        (self.root / 'base.json').write_text(json.dumps(base))
        old = Experiment.load(self.path)
        with Cohort(old):
            pass
        before = self.path.read_bytes()
        ledger = Path(next(iter(old.scenarios.values())).state_file)
        original = ledger.read_bytes()
        backup = upgrade_center(self.path)
        updated = Experiment.load(self.path)
        self.assertEqual(updated.center_hours, 72)
        self.assertEqual(updated.base.center_hours, 168)
        self.assertEqual(updated.output.name, 'comparison-center3d')
        self.assertTrue(all(c.quantity_barrels == '2' and c.paper_balance_usdc == '1700' for c in updated.scenarios.values()))
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(ledger.read_bytes(), original)
        self.assertIsNone(upgrade_center(self.path))
        with Cohort(updated) as cohort:
            self.assertIsNone(cohort.latest())

    def test_single_migration_and_collision_preserve_data(self):
        path = self.root / 'base.json'
        config = asdict(Config(center_hours=168))
        config.pop('center_hours')
        path.write_text(json.dumps(config))
        target = self.root / 'data/paper-center3d.sqlite3'
        target.parent.mkdir()
        target.write_bytes(b'original data')
        before = path.read_bytes()
        with self.assertRaises(GridError):
            upgrade_center(path, comparison=False)
        self.assertEqual(path.read_bytes(), before)
        target.unlink()
        backup = upgrade_center(path, comparison=False)
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(configuration(path).center_hours, 72)
        self.assertEqual(Path(configuration(path).state_file), target.resolve())
        self.assertIsNone(upgrade_center(path, comparison=False))

    def test_single_first_upgrade_still_migrates_existing_seven_day_comparison(self):
        path = self.root / 'base.json'
        path.write_text(json.dumps(asdict(Config(center_hours=168))))
        old = Experiment.load(self.path)
        with Cohort(old) as cohort:
            cohort.ingest(frame())
        manifest = (old.output / 'experiment.json').read_bytes()
        upgrade_center(path, comparison=False)
        self.assertEqual(Experiment.load(self.path).center_hours, 72)
        self.assertIsNotNone(upgrade_center(self.path))
        new = Experiment.load(self.path)
        self.assertNotEqual(new.output, old.output)
        self.assertEqual((old.output / 'experiment.json').read_bytes(), manifest)
        with Cohort(new) as cohort:
            self.assertIsNone(cohort.latest())
        self.assertIsNone(upgrade_center(self.path))

    def test_reset_archives_every_ledger_and_restores_cash_and_all_counters(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
            cohort.ingest(frame('6.3', 1))
            before = read_dashboard(self.experiment)
            for store in cohort.stores.values():
                with store.transaction():
                    store.set('halted', 'max_drawdown')
            old_generation = read_state(self.experiment)['generation']
            first = request_reset(self.experiment, old_generation)
            self.assertEqual(first, request_reset(self.experiment, old_generation))
            self.assertTrue(process_reset(cohort))
            self.assertFalse(process_reset(cohort))
            state = read_state(self.experiment)
            self.assertNotEqual(state['generation'], old_generation)
            self.assertIsNone(cohort.latest())
            data = read_dashboard(self.experiment)
            self.assertEqual(data['positions'], [])
            self.assertEqual(data['history']['points'], [])
            with self.assertRaises(GridError):
                request_reset(self.experiment, old_generation)
            for name, store in cohort.stores.items():
                self.assertFalse(store.lots())
                self.assertEqual(store.get('cash'), self.experiment.scenarios[name].paper_balance_usdc)
                self.assertEqual(store.get('peak'), store.get('cash'))
                self.assertEqual(store.get('halted'), '')
                self.assertIsNone(store.get('last_tick'))
                self.assertEqual(store.volume(), {'volume_barrels':'0', 'turnover_usdc':'0', 'fill_count':0})
                archive = self.experiment.output / 'archives' / state['archive_id'] / 'ledgers' / (name+'.sqlite3')
                with closing(sqlite3.connect(archive)) as db:
                    self.assertGreater(db.execute('SELECT COUNT(*) FROM fills').fetchone()[0], 0)
            with closing(sqlite3.connect(self.experiment.output / 'archives' / state['archive_id'] / 'comparison.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], before['summary']['sample_count'])
            result = cohort.ingest(frame('7', 2))
            self.assertEqual(result['sample_count'], 1)
            self.assertTrue(all(r['fill_count'] == 0 and r['total_pnl_usdc'] == '0' for r in result['scenarios']))

    def test_archive_failure_leaves_original_round_usable(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
            before = cohort.latest()
            request_reset(self.experiment, read_state(self.experiment)['generation'])
            with patch('variational_grid.reset.backup_database', side_effect=OSError('disk full')):
                self.assertFalse(process_reset(cohort))
            self.assertEqual(cohort.latest(), before)
            self.assertTrue(all(s.lots() for s in cohort.stores.values()))
            self.assertEqual(read_state(self.experiment)['status'], 'failed')

    def test_runner_processes_reset_before_closed_market_and_without_orders(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
        request_reset(self.experiment, read_state(self.experiment)['generation'])
        class FakeFeed:
            class client:
                @staticmethod
                def check_session():
                    return {'authenticated': True}
            def next(self):
                raise GridError('Market closed')
        with patch('variational_grid.comparison.MarketFeed', return_value=FakeFeed()), redirect_stdout(io.StringIO()):
            self.assertEqual(run_comparison(argparse.Namespace(experiments=self.path, once=True, iterations=0)), 2)
        self.assertEqual(read_state(self.experiment)['status'], 'complete')
        data=read_dashboard(self.experiment)
        self.assertIsNone(data['summary'])
        self.assertFalse(data['positions'])

    def test_interrupted_partial_reset_finishes_before_recovery_or_publication(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
            request_reset(self.experiment, read_state(self.experiment)['generation'])
            second = list(cohort.stores.values())[1]
            with patch.object(second, 'reset', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    process_reset(cohort)
            data = read_dashboard(self.experiment)
            self.assertEqual(data['runtime']['status'], 'resetting')
            self.assertIsNone(data['summary'])
        with Cohort(self.experiment) as cohort:
            self.assertIsNone(cohort.latest())
            self.assertTrue(all(not s.lots() for s in cohort.stores.values()))
            self.assertEqual(read_state(self.experiment)['status'], 'complete')

    def test_reset_http_requires_origin_token_generation_and_valid_json(self):
        with Cohort(self.experiment) as cohort, make_server(self.experiment, 0) as server:
            cohort.ingest(frame())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, path, body=None, headers=None):
                with closing(http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)) as connection:
                    connection.request(method,path,body=body,headers=headers or {})
                    response=connection.getresponse()
                    return response.status,response.read()
            try:
                code, raw = request('GET','/api/dashboard')
                data=json.loads(raw)
                body=json.dumps({'generation':data['reset']['generation']})
                headers={'Content-Type':'application/json','X-Reset-Token':data['reset_token']}
                self.assertEqual(request('POST','/api/reset',body,{})[0],403)
                self.assertEqual(request('POST','/api/reset',body,{**headers,'Origin':'https://attacker.invalid'})[0],403)
                self.assertEqual(request('POST','/api/reset',body,{**headers,'Host':'attacker.invalid'})[0],403)
                self.assertEqual(request('POST','/api/reset','{}',headers)[0],400)
                self.assertEqual(request('POST','/api/reset',json.dumps({'generation':'old'}),headers)[0],409)
                self.assertEqual(request('GET','/api/reset')[0],404)
                good={**headers,'Origin':f'http://127.0.0.1:{server.server_port}'}
                self.assertEqual(request('POST','/api/reset',body,good)[0],202)
                self.assertTrue(cohort.latest())  # Web thread only queued the request.
                self.assertTrue(process_reset(cohort))
                self.assertEqual(request('POST','/api/reset',body,good)[0],409)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_position_limits_are_gross_both_legs_and_do_not_compound_profits(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
            for row in read_dashboard(self.experiment)['summary']['scenarios']:
                self.assertEqual(D(row['position_notional_usdc']),D(row['margin_usdc'])*5)
                self.assertEqual(D(row['entry_notional_limit_usdc']),D(row['equity_usdc'])*D('.8')*5)
            engine=next(iter(cohort.engines.values()))
            self.assertEqual(D(engine.position_limits(D(1100),D(0))['entry_notional_limit_usdc']),D(4000))
            self.assertEqual(D(engine.position_limits(D(800),D(0))['entry_notional_limit_usdc']),D(3200))


if __name__ == '__main__':
    unittest.main()
