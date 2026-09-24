"""QQQ dashboard routing reuses the existing loopback, CSP and reset boundary."""
from contextlib import closing, contextmanager
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from variational_grid.dashboard import make_server, read_dashboard
from variational_grid.models import GridError


class QQQDashboardRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.experiment = SimpleNamespace(kind="qqq_hedge", output=Path(self.temp.name))
        self.payload = {"kind": "qqq_hedge", "runtime": {"status": "starting"}, "summary": None,
                        "history": {"range": "24h", "names": [], "source_count": 0, "points": []},
                        "positions": [], "trades": [], "details_available": False, "reset": None}

    def test_qqq_reader_runs_inside_existing_control_lock(self):
        active = False

        @contextmanager
        def lock(experiment):
            nonlocal active
            self.assertIs(experiment, self.experiment)
            active = True
            try:
                yield
            finally:
                active = False

        def reader(experiment, window):
            self.assertTrue(active)
            self.assertIs(experiment, self.experiment)
            self.assertEqual(window, "7d")
            return self.payload

        module = SimpleNamespace(read_qqq_dashboard=Mock(side_effect=reader))
        with patch.dict(sys.modules, {"variational_grid.qqq_comparison": module}), \
                patch("variational_grid.dashboard.control_lock", lock), \
                patch("variational_grid.dashboard._read_dashboard") as legacy:
            self.assertIs(read_dashboard(self.experiment, "7d"), self.payload)
            self.assertFalse(active)
            legacy.assert_not_called()
            with self.assertRaises(GridError):
                read_dashboard(self.experiment, "bad")
            self.assertEqual(module.read_qqq_dashboard.call_count, 1)

    def test_static_assets_history_and_reset_reuse_existing_boundary(self):
        with make_server(self.experiment, 0) as server:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(path, method="GET", body=None, headers=None):
                    with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as connection:
                        connection.request(method, path, body=body, headers=headers or {})
                        response = connection.getresponse()
                        return response.status, dict(response.getheaders()), response.read()

                for path in ("/", "/index.html", "/qqq.js", "/qqq.css", "/model.js"):
                    status, headers, body = request(path)
                    self.assertEqual(status, 200, path)
                    self.assertTrue(body)
                    self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                    self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
                    if path in ("/", "/index.html"):
                        self.assertIn(b"/qqq.js", body)
                        self.assertNotIn(b"/app.js", body)
                        self.assertNotIn(b"/inventory.js", body)
                self.assertEqual(request("/", "HEAD")[2], b"")
                for path in ("/qqq.html", "/comparison.sqlite3", "/../qqq.py"):
                    self.assertEqual(request(path)[0], 404)
                self.assertEqual(request("/", headers={"Host": "attacker.invalid"})[0], 403)
                self.assertEqual(request("/api/dashboard?range=30d")[0], 400)
                self.assertEqual(request("/api/dashboard?path=secret")[0], 400)
                with patch("variational_grid.dashboard.read_dashboard", return_value=dict(self.payload)) as reader:
                    status, _, body = request("/api/dashboard?range=7d")
                    self.assertEqual(status, 200)
                    reader.assert_called_once_with(self.experiment, "7d")
                    token = json.loads(body)["reset_token"]
                with patch("variational_grid.dashboard.request_reset", return_value={"status": "pending"}) as reset:
                    body = json.dumps({"generation": "generation-1"})
                    headers = {"Content-Type": "application/json", "X-Reset-Token": token}
                    self.assertEqual(request("/api/reset", "POST", body, {"Content-Type": "application/json"})[0], 403)
                    self.assertEqual(request("/api/reset", "POST", body, {**headers, "Origin": "https://attacker.invalid"})[0], 403)
                    reset.assert_not_called()
                    self.assertEqual(request("/api/reset", "POST", body, headers)[0], 202)
                    reset.assert_called_once_with(self.experiment, "generation-1")
                with patch("variational_grid.dashboard.read_dashboard", side_effect=GridError("secret path")):
                    status, _, body = request("/api/dashboard")
                    self.assertEqual(status, 503)
                    self.assertNotIn(b"secret", body)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_other_experiments_do_not_serve_qqq_assets(self):
        for kind, script in ((None, b"/app.js"), ("inventory", b"/inventory.js")):
            experiment = SimpleNamespace(kind=kind, output=self.experiment.output)
            with make_server(experiment, 0) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as connection:
                        connection.request("GET", "/")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        body = response.read()
                        self.assertIn(script, body)
                        self.assertNotIn(b"/qqq.js", body)
                        connection.request("GET", "/qqq.js")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 404)
                        response.read()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
