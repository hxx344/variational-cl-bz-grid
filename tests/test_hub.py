import base64
from contextlib import closing
import copy
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from variational_grid.dashboard import make_server
from variational_grid.hub import read_summary
from variational_grid.models import utc


class HubTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.experiment = SimpleNamespace(output=Path(temporary.name) / 'paper', kind='comparison')
        self.experiment.output.mkdir()
        self.now = time.time()
        self.sample = {'mode': 'paper_comparison', 'ts': self.now - 2, 'sample_count': 8, 'poll_seconds': 10,
                       'scenarios': [{'name': 'A', 'total_pnl_usdc': '12.34'}, {'name': 'B', 'total_pnl_usdc': '-5'}]}

    def save(self, sample=None, status='running', compressed=False):
        sample = copy.deepcopy(self.sample if sample is None else sample)
        raw = json.dumps(sample)
        if compressed:
            raw = json.dumps({'qqq_compact': 1, 'state': base64.b64encode(zlib.compress(raw.encode())).decode()})
        with closing(sqlite3.connect(self.experiment.output / 'comparison.sqlite3')) as db, db:
            db.executescript('CREATE TABLE IF NOT EXISTS runtime(id PRIMARY KEY,payload); CREATE TABLE IF NOT EXISTS summaries(ts PRIMARY KEY,payload); DELETE FROM summaries;')
            db.execute('INSERT OR REPLACE INTO runtime VALUES(1,?)', (json.dumps({'status': status, 'reason': 'private/session.json vr-token SECRET', 'pid': 123}),))
            db.execute('INSERT INTO summaries VALUES(?,?)', (self.now, raw))

    def read(self):
        with (patch('variational_grid.dashboard.read_dashboard', side_effect=AssertionError('No heavy dashboard')),
              patch('variational_grid.client.Client.request', side_effect=AssertionError('No venue call'))):
            return read_summary(self.experiment, now=self.now)['data']

    def test_published_modes_and_compressed_qqq_keep_independent_paper_values(self):
        for mode in ('paper_comparison', 'inventory_comparison', 'qqq_hedge_comparison'):
            for compressed in (False, True):
                with self.subTest(mode=mode, compressed=compressed):
                    sample = copy.deepcopy(self.sample)
                    sample['mode'] = mode
                    sample['market'] = {'qqq_source_ts': self.now - 3, 'source_status': 'ready'}
                    for row in sample['scenarios']:
                        row.update(us100={'qty': '-1'}, var_valued_at=self.now - 4)
                    self.save(sample, compressed=compressed)
                    data = self.read()
                    self.assertEqual(data['health']['state'], 'online')
                    self.assertEqual(data['updatedAt'], utc(self.now - (4 if mode == 'qqq_hedge_comparison' else 2)))
                    self.assertEqual([m['value'] for m in data['metrics'][3:]], [12.34, -5])
                    self.assertTrue(all(m['unit'] == 'USDC' for m in data['metrics'][3:]))
                    for private in ('SECRET', 'vr-token', 'session.json', 'pid', str(self.experiment.output)):
                        self.assertNotIn(private, json.dumps(data))

    def test_no_sample_reset_stopped_pause_and_age(self):
        self.assertIsNone(self.read()['updatedAt'])
        for status, expected in [('running', 'online'), ('degraded', 'partial'), ('paused', 'partial'), ('stopped', 'offline'), ('unknown', 'partial')]:
            self.save(status=status)
            self.assertEqual(self.read()['health']['state'], expected)
        self.sample['ts'] = self.now - 90
        self.save()
        self.assertEqual(self.read()['health']['state'], 'stale')
        for status in ('archiving', 'clearing'):
            (self.experiment.output / 'reset-state.json').write_text(json.dumps({'status': status}))
            data = self.read()
            self.assertIsNone(data['updatedAt'])
            self.assertEqual(data['metrics'], [])

    def test_old_or_missing_us100_valuation_cannot_be_refreshed_by_new_qqq_tick(self):
        self.sample.update(mode='qqq_hedge_comparison', market={'qqq_source_ts': self.now, 'source_status': 'ready'})
        for row in self.sample['scenarios']:
            row.update(us100={'qty': '-1'}, var_valued_at=self.now - 100)
        self.save()
        self.assertEqual(self.read()['health']['state'], 'stale')
        self.assertEqual(self.read()['updatedAt'], utc(self.now - 100))
        self.sample['scenarios'][0]['var_valued_at'] = None
        self.save()
        self.assertIsNone(self.read()['updatedAt'])

    def test_missing_values_future_time_synthetic_and_metric_limit(self):
        for value in (None, '', 'NaN', 'Infinity', True):
            self.sample['scenarios'][0]['total_pnl_usdc'] = value
            self.save()
            self.assertIsNone(self.read()['metrics'][3]['value'])
            self.assertEqual(self.read()['health']['state'], 'partial')
        self.sample.update(ts=self.now + 120, data_kind='synthetic')
        self.save()
        self.assertIsNone(self.read()['updatedAt'])
        self.assertIn('合成行情演示', self.read()['health']['message'])
        self.sample['scenarios'] *= 15
        self.save()
        self.assertEqual(len(self.read()['metrics']), 24)

    def test_http_summary_preserves_loopback_boundary_and_hides_read_errors(self):
        self.save()
        with make_server(self.experiment, 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(path, host=None, method='GET'):
                    with closing(http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)) as connection:
                        connection.request(method, path, headers={'Host': host} if host else {})
                        response = connection.getresponse()
                        return response.status, dict(response.getheaders()), response.read()
                for path in ('/api/hub/summary', '/api/hub/summary?schemaVersion=2'):
                    status, headers, body = request(path)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers['Cache-Control'], 'no-store')
                    self.assertEqual(json.loads(body)['schemaVersion'], 2)
                self.assertEqual(request('/api/hub/summary', host='attacker.invalid')[0], 403)
                self.assertEqual(request('/api/hub/summary?schemaVersion=3')[0], 400)
                self.assertEqual(request('/api/hub/summary', method='HEAD')[2], b'')
                self.assertEqual(request('/hub.js')[0], 200)
                with patch('variational_grid.hub.read_summary', side_effect=ValueError('private token')):
                    status, _, body = request('/api/hub/summary')
                    self.assertEqual(status, 503)
                    self.assertNotIn(b'private', body)
            finally:
                server.shutdown()
                thread.join(timeout=5)
