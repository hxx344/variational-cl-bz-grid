from contextlib import closing, redirect_stderr
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from variational_grid.client import Client, save_session
from variational_grid.dashboard import make_server, _SubmittedSession, VarSessionControl, SessionUpdateError
from variational_grid.models import GridError
from variational_grid.qqq_market import VarSwapClient
from variational_grid.qqq_pricing import ReferenceCache, QQQPricing
from variational_grid.qqq_comparison import write_export
from test_qqq_auth import token
from test_qqq_market import NOW, Opener, Response, var_metadata, var_quote


class DashboardSessionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / 'private/session.json'
        self.old = token(NOW + 3600, 'old')
        self.new = token(NOW + 7200, 'new')
        save_session(self.path, {'token': self.old})
        clock = patch('variational_grid.client.time.time', return_value=NOW)
        clock.start(); self.addCleanup(clock.stop)
        self.experiment = SimpleNamespace(kind='qqq_hedge', output=self.root / 'paper',
                                          base=SimpleNamespace(session_file=str(self.path)))
        self.server = make_server(self.experiment, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.csrf = json.loads(self.request()[2])['csrf_token']

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)

    def request(self, method='GET', body=None, headers=None, path='/api/var-session'):
        base = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Session-Token': getattr(self, 'csrf', '')}
        if headers:
            base.update(headers)
        base = {k:v for k,v in base.items() if v is not None}
        with closing(http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)) as connection:
            connection.request(method, path, body, base)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()

    def submit(self, value=None, **kwargs):
        return self.request('POST', json.dumps({'token': value or self.new}), **kwargs)

    def test_status_and_success_are_secret_free_and_atomic(self):
        before = self.path.read_bytes()
        status, headers, raw = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(raw)['state'], 'stored')
        logs = io.StringIO()
        with redirect_stderr(logs), patch.object(_SubmittedSession, 'check_session', return_value={'authenticated':True}) as verify:
            status, _, raw = self.submit()
        self.assertEqual(status, 200)
        verify.assert_called_once()
        self.assertNotEqual(self.path.read_bytes(), before)
        self.assertEqual(Client(self.path).session()[0], self.new)
        self.assertEqual(json.loads(raw)['session']['state'], 'stored')
        for secret in (self.old, self.new, str(self.path)):
            self.assertNotIn(secret, raw.decode() + logs.getvalue())
        self.assertEqual(self.request(path='/session.json')[0], 404)
        self.assertEqual(self.request(path='/api/var-session?token='+self.new)[0], 404)

    def test_verification_uses_only_fixed_me_endpoint_and_candidate_cookie(self):
        opener = Opener([Response({'token': self.new})])
        with patch('variational_grid.client.urllib.request.build_opener', return_value=opener):
            self.assertEqual(self.submit()[0], 200)
        self.assertEqual(len(opener.requests), 1)
        req = opener.requests[0]
        self.assertEqual((req.method, req.full_url), ('GET','https://omni.variational.io/api/me'))
        self.assertEqual(req.get_header('Cookie'), 'vr-token='+self.new)
        self.assertIsNone(req.data)

    def test_origin_host_csrf_and_fetch_metadata_rejections_never_verify(self):
        original = self.path.read_bytes()
        for headers in ({'Origin':None}, {'Origin':'https://attacker.invalid'}, {'X-Session-Token':None},
                        {'X-Session-Token':'wrong'}, {'X-Session-Token':'é'}, {'Sec-Fetch-Site':'cross-site'},
                        {'Host':'attacker.invalid'}, {'Host':'localhost:bad'}, {'Host':'user@localhost'},
                        {'Host':'localhost/secret'}, {'Host':'localhost?x=1'}):
            with self.subTest(headers=headers), patch.object(_SubmittedSession,'check_session') as verify:
                self.assertEqual(self.submit(headers=headers)[0],403)
                verify.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_json_shape_expiry_and_transport_never_touch_current_session(self):
        original = self.path.read_bytes()
        cases = ['not-json', '[]', json.dumps({'token':True}), json.dumps({'token':self.new,'path':'/tmp/other'}),
                 json.dumps({'token':'vr-token='+self.new}), json.dumps({'token':token(NOW+30)}),
                 json.dumps({'token':'x'*9001})]
        with patch.object(_SubmittedSession, 'check_session') as verify:
            for body in cases:
                self.assertEqual(self.request('POST',body)[0],400)
            for headers in ({'Content-Type':'text/plain'}, {'Transfer-Encoding':'chunked'}, {'Content-Length':'bogus'}):
                self.assertEqual(self.submit(headers=headers)[0],400)
            verify.assert_not_called()
        self.assertEqual(self.path.read_bytes(),original)

    def test_failed_remote_validation_and_failed_write_preserve_old_session(self):
        original = self.path.read_bytes()
        with patch.object(_SubmittedSession,'check_session', side_effect=GridError(self.new)):
            status, _, raw = self.submit()
        self.assertEqual(status,422)
        self.assertNotIn(self.new,raw.decode())
        self.assertEqual(self.path.read_bytes(),original)

    def test_other_strategy_modes_do_not_enable_session_update(self):
        for kind in ('inventory', None):
            control = VarSessionControl(SimpleNamespace(kind=kind,base=self.experiment.base))
            self.assertFalse(control.status()['enabled'])
            with patch.object(_SubmittedSession,'check_session') as verify:
                with self.assertRaises(SessionUpdateError) as caught:
                    control.update({'token':self.new})
                self.assertEqual(caught.exception.status,405)
                verify.assert_not_called()

    def test_save_failure_is_sanitized(self):
        original = self.path.read_bytes()
        with patch.object(_SubmittedSession,'check_session',return_value={'authenticated':True}), \
                patch('variational_grid.dashboard.save_session',side_effect=GridError(str(self.path)+self.new)):
            status, _, raw = self.submit()
        self.assertEqual(status,503)
        self.assertNotIn(self.new,raw.decode())
        self.assertNotIn(str(self.path),raw.decode())
        self.assertEqual(self.path.read_bytes(),original)

    def test_repeated_submission_is_throttled_without_extra_remote_request(self):
        with patch.object(_SubmittedSession,'check_session',return_value={'authenticated':True}) as verify:
            self.assertEqual(self.submit()[0],200)
            self.assertEqual(self.submit()[0],429)
            verify.assert_called_once()

    def test_missing_session_can_be_repaired_without_dashboard_ledger(self):
        self.path.unlink()
        self.assertFalse(self.experiment.output.exists())
        self.assertEqual(json.loads(self.request()[2])['state'],'unavailable')
        with patch.object(_SubmittedSession,'check_session',return_value={'authenticated':True}):
            self.assertEqual(self.submit()[0],200)
        self.assertEqual(Client(self.path).session()[0],self.new)
        self.assertFalse(self.experiment.output.exists())

    def test_existing_engine_reloads_new_saved_token_and_requires_new_quote(self):
        now = [NOW]
        opener = Opener([Response(var_metadata()),Response(var_quote())])
        client = VarSwapClient(session_file=self.path,opener=opener,clock=lambda:now[0],max_age_seconds=60)
        cache = ReferenceCache(client,self.root/'cache.json',QQQPricing(),write_export)
        self.assertTrue(cache.read()[1]['available'])
        with patch.object(_SubmittedSession,'check_session',return_value={'authenticated':True}):
            self.assertEqual(self.submit()[0],200)
        self.assertIsNone(cache.usable(NOW))
        now[0] += 4
        opener.responses = [Response(var_quote(now=now[0]),now[0])]
        self.assertTrue(cache.read()[1]['available'])
        self.assertEqual(opener.requests[-1].get_header('Cookie'),'vr-token='+self.new)


if __name__ == '__main__':
    unittest.main()
