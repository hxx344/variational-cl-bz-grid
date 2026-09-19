from contextlib import closing
from dataclasses import asdict, replace
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from variational_grid.comparison import Cohort, Experiment, Frame
from variational_grid.dashboard import make_server, read_dashboard, read_db, reduce_points
from variational_grid.models import Config, D, GridError, HOUR, Quote


BASE = 1735689600


def frame(spread="7.6", step=0):
    ts = BASE + step * 10
    quotes = tuple(Quote(symbol, mark-D('.02'), mark+D('.02'), mark, D(1), ts)
                   for symbol, mark in (("CL", D(95)), ("BZ", D(95)+D(spread))))
    return Frame(ts, int(ts)//HOUR*HOUR, D(7), {"1": quotes})


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'base.json').write_text(json.dumps(asdict(Config(fee_bps_per_leg='1'))))
        self.spec = {'base_config': 'base.json', 'output_dir': 'comparison', 'scenarios': [
            {'name': 'step-'+step, 'overrides': {'grid_step_usdc_per_barrel': step}}
            for step in ('0.15', '0.20', '0.25')]}
        self.path = self.root / 'experiments.json'
        self.path.write_text(json.dumps(self.spec))
        self.experiment = Experiment.load(self.path)

    def seed(self):
        with Cohort(self.experiment) as cohort:
            for i, spread in enumerate(('7.6', '7.7', '6.4', '6.3')):
                cohort.ingest(frame(spread, i))

    def test_positions_reconcile_with_equity_and_realized_after_fees(self):
        self.seed()
        with patch('variational_grid.client.Client.request', side_effect=AssertionError('No venue calls')):
            data = read_dashboard(self.experiment)
        self.assertTrue(data['details_available'])
        self.assertTrue(data['positions'])
        self.assertTrue(data['trades'])
        for row in data['summary']['scenarios']:
            lots = [p for p in data['positions'] if p['scenario'] == row['name']]
            self.assertEqual(len(lots), row['open_pairs'])
            floating = sum((D(p['unrealized_pnl_usdc']) for p in lots), D(0))
            self.assertEqual(floating, D(row['total_pnl_usdc']) - D(row['realized_pnl_usdc']))
            self.assertTrue(all(p['direction'] == 1 for p in lots))
            self.assertEqual(sum(D(t['net_pnl']) for t in data['trades'] if t['scenario'] == row['name']), D(row['realized_pnl_usdc']))
        raw = json.dumps(data)
        for secret in ('session_file', 'vr-token', str(self.root), 'state_file', '"pid"'):
            self.assertNotIn(secret, raw)

    def test_ledger_ahead_of_published_frame_does_not_leak_future_positions(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
            before = read_dashboard(self.experiment)
            engine = cohort.engines['step-0.15']
            for i in (1, 2):
                next_frame = frame('6.3', i)
                engine.tick(next_frame.center, *next_frame.quotes['1'], next_frame.ts)
            after = read_dashboard(self.experiment)
            self.assertEqual(before['positions'], after['positions'])
            self.assertEqual(before['trades'], after['trades'])
            self.assertEqual(before['summary'], after['summary'])
            self.assertEqual(before['history'], after['history'])

    def test_older_or_reconfigured_ledger_is_refused(self):
        self.seed()
        config = self.experiment.scenarios['step-0.15']
        self.experiment.scenarios['step-0.15'] = replace(config, fee_bps_per_leg='9')
        with self.assertRaises(GridError):
            read_dashboard(self.experiment)
        self.experiment.scenarios['step-0.15'] = config
        with closing(sqlite3.connect(config.state_file)) as db:
            db.execute("UPDATE meta SET value='0' WHERE key='last_tick'")
            db.commit()
        with self.assertRaises(GridError):
            read_dashboard(self.experiment)

    def test_waiting_and_missing_frame_are_not_reported_as_flat_positions(self):
        data = read_dashboard(self.experiment)
        self.assertIsNone(data['summary'])
        self.assertFalse(self.experiment.output.exists())
        self.seed()
        with closing(sqlite3.connect(self.experiment.output / 'comparison.sqlite3')) as db:
            db.execute('DELETE FROM frames')
            db.commit()
        data = read_dashboard(self.experiment)
        self.assertIsNotNone(data['summary'])
        self.assertFalse(data['details_available'])

    def test_history_window_time_gaps_and_reordered_scenarios(self):
        with Cohort(self.experiment) as cohort:
            cohort.ingest(frame())
            old = cohort.ingest(frame('7.7', 1))
            new = cohort.ingest(frame('7.8', 400))
            new['scenarios'].reverse()
            cohort.db.execute('UPDATE summaries SET payload=? WHERE ts=?', (json.dumps(new), new['ts']))
            cohort.db.commit()
        data = read_dashboard(self.experiment, '1h')
        self.assertEqual(data['history']['source_count'], 1)
        history = read_dashboard(self.experiment, '24h')['history']
        self.assertEqual([p['segment'] for p in history['points']], [0, 0, 1])
        self.assertEqual(history['points'][1]['pnl'], [float(r['total_pnl_usdc']) for r in reversed(old['scenarios'])])

    def test_downsampling_preserves_global_extrema_endpoints_and_gap_segments(self):
        points = [{'ts': i, 'spread': i%19, 'center': 7, 'pnl': [i%23, -(i%29), i%31], 'segment': int(i>=2000)} for i in range(6000)]
        points[2021]['pnl'][1] = -99
        sampled = reduce_points(points)
        self.assertLessEqual(len(sampled), 900)
        self.assertEqual(sampled[0], points[0])
        self.assertEqual(sampled[-1], points[-1])
        self.assertIn(points[2021], sampled)
        self.assertEqual({p['segment'] for p in sampled}, {0, 1})
        for key in ('spread', 'center'):
            self.assertEqual(min(p[key] for p in sampled), min(p[key] for p in points))
            self.assertEqual(max(p[key] for p in sampled), max(p[key] for p in points))
        for i in range(3):
            self.assertEqual(min(p['pnl'][i] for p in sampled), min(p['pnl'][i] for p in points))
            self.assertEqual(max(p['pnl'][i] for p in sampled), max(p['pnl'][i] for p in points))

    def test_connections_cannot_write_the_ledger(self):
        self.seed()
        with closing(read_db(self.experiment.output / 'comparison.sqlite3')) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM frames')

    def test_http_routes_are_loopback_read_only_and_do_not_serve_files(self):
        self.seed()
        with make_server(self.experiment, 0) as server:
            self.assertEqual(server.server_address[0], '127.0.0.1')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(path, method='GET', host=None):
                    with closing(http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)) as connection:
                        connection.request(method, path, headers={'Host': host} if host else {})
                        response = connection.getresponse()
                        return response.status, dict(response.getheaders()), response.read()
                for path in ('/', '/app.js', '/model.js', '/styles.css', '/api/dashboard?range=7d', '/report'):
                    status, headers, body = request(path)
                    self.assertEqual(status, 200, path)
                    self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
                    self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])
                    self.assertTrue(body)
                    if path == '/report':
                        self.assertIn('sha256-', headers['Content-Security-Policy'])
                        self.assertNotIn(b'\r', body)
                for path in ('/session.json', '/config.json', '/comparison.sqlite3', '/../../session.json', '/%2e%2e/session.json', '/ledgers/step-0.15.sqlite3'):
                    self.assertEqual(request(path)[0], 404)
                self.assertEqual(request('/api/dashboard?range=30d')[0], 400)
                self.assertEqual(request('/api/dashboard?file=session.json')[0], 400)
                self.assertEqual(request('/api/dashboard', 'POST')[0], 405)
                self.assertEqual(request('/api/dashboard', host='attacker.invalid')[0], 403)
                self.assertEqual(request('/', 'HEAD')[2], b'')
                with patch('variational_grid.dashboard.read_dashboard', side_effect=OSError('private/path/session.json')):
                    status, _, body = request('/api/dashboard')
                    self.assertEqual(status, 503)
                    self.assertNotIn(b'private', body)
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
