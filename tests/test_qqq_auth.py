import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from variational_grid.client import save_session, NoRedirect, ORIGIN
from variational_grid.models import GridError
from variational_grid.qqq_auth import SessionUnavailable
from variational_grid.qqq_market import VarSwapClient, LighterClient, VAR_QUOTE_SOURCE, _Transport
from variational_grid.qqq_pricing import ReferenceCache, QQQPricing
from variational_grid.qqq_comparison import write_export
from test_qqq_market import NOW, Opener, Response, var_metadata, var_quote, frame, trade


def token(expiry, signature="first"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return "fixture." + payload + "." + signature


class AuthenticatedQuotesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "session.json"
        self.now = [NOW]
        clock = patch("variational_grid.client.time.time", side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.secret = token(NOW + 3600)
        self.write_token(self.secret)
        self.opener = Opener([Response(var_metadata()), Response(var_quote())])
        self.client = VarSwapClient(session_file=self.path, opener=self.opener,
                                    clock=lambda: self.now[0], max_age_seconds=60)
        self.cache_path = self.path.parent / "quote-cache.json"
        self.cache = ReferenceCache(self.client, self.cache_path, QQQPricing(), write_export)

    def write_token(self, value):
        save_session(self.path, {"token": value, "user_agent": "fixture-UA"})

    def test_fixed_auth_requests_and_public_exports_contain_no_credential(self):
        quote, status = self.cache.read()
        self.assertEqual(quote["source"], VAR_QUOTE_SOURCE)
        self.assertTrue(status["authenticated"])
        self.assertEqual([r.full_url for r in self.opener.requests], [
            ORIGIN + "/api/metadata/supported_assets?cex_asset=US100S", ORIGIN + "/api/quotes/indicative"])
        for request in self.opener.requests:
            self.assertEqual(request.get_header("Cookie"), "vr-token=" + self.secret)
            self.assertEqual(request.get_header("User-agent"), "fixture-UA")
        public = json.dumps([quote, status, self.client.transport.state()]) + self.cache_path.read_text()
        self.assertNotIn(self.secret, public)
        self.assertNotIn("Cookie", public)
        lighter = Opener(frame([trade(1)]))
        LighterClient(opener=lighter, clock=lambda: NOW).snapshot()
        self.assertTrue(all(r.get_header("Cookie") is None for r in lighter.requests))

    def test_missing_expiring_and_unreadable_session_blocks_even_fresh_cache(self):
        for state in ("missing", "expired", "corrupt"):
            with self.subTest(state=state):
                self.write_token(self.secret)
                self.opener.responses = [Response(var_quote())]
                if not self.client._market:
                    self.opener.responses.insert(0, Response(var_metadata()))
                self.now[0] += 4
                self.opener.responses[-1] = Response(var_quote(now=self.now[0]), self.now[0])
                self.assertIsNotNone(self.cache.read()[0])
                count = len(self.opener.requests)
                if state == "missing":
                    self.path.unlink()
                elif state == "expired":
                    self.write_token(token(self.now[0] + 30))
                else:
                    self.path.write_text("invalid")
                current, status = self.cache.read()
                self.assertIsNone(current)
                self.assertIsNone(self.cache.usable(self.now[0]))
                self.assertFalse(status["authenticated"])
                self.assertIn("init-session", status["refresh_error"])
                self.assertEqual(len(self.opener.requests), count)

    def test_server_rejection_disables_cache_until_rotated_token_succeeds(self):
        for code in (401, 403):
            with self.subTest(code=code):
                self.write_token(token(NOW + 3600, str(code)))
                self.now[0] += 4
                self.opener.responses = [Response(var_quote(now=self.now[0]), self.now[0])]
                if not self.client._market:
                    self.opener.responses.insert(0, Response(var_metadata()))
                self.cache.read()
                self.now[0] += 4
                self.opener.responses = [urllib.error.HTTPError(ORIGIN, code, self.secret, {}, io.BytesIO(self.secret.encode()))]
                current, status = self.cache.read()
                self.assertIsNone(current)
                self.assertIn("401/403", status["refresh_error"])
                self.assertNotIn(self.secret, json.dumps(status))
                count = len(self.opener.requests)
                self.now[0] += 4
                self.assertIsNone(self.cache.read()[0])
                self.assertIsNone(self.cache.usable(self.now[0]))
                self.assertEqual(len(self.opener.requests), count)
                new = token(NOW + 3600, "replacement" + str(code))
                self.write_token(new)
                self.opener.responses = [Response(var_quote(now=self.now[0]), self.now[0])]
                current, status = self.cache.read()
                self.assertIsNotNone(current)
                self.assertTrue(status["authenticated"])
                self.assertEqual(self.opener.requests[-1].get_header("Cookie"), "vr-token=" + new)

    def test_old_public_cache_is_not_relabelled_and_restart_needs_confirmation(self):
        self.cache.read()
        raw = json.loads(self.cache_path.read_text())
        other = VarSwapClient(session_file=self.path, opener=self.opener, clock=lambda: self.now[0])
        persisted = ReferenceCache(other, self.cache_path, QQQPricing(), write_export)
        self.assertEqual(persisted.quote["ts"], NOW)
        self.assertIsNone(persisted.usable(NOW))
        raw["quote"]["source"] = "variational_simple_indicative"
        self.cache_path.write_text(json.dumps(raw))
        legacy = ReferenceCache(other, self.cache_path, QQQPricing(), write_export)
        self.assertIsNone(legacy.quote)
        self.assertIn("旧公共报价", legacy.restore_error)
        self.opener.responses = [Response(var_metadata()), Response(var_quote())]
        current, _ = legacy.read()
        self.assertEqual(current["source"], VAR_QUOTE_SOURCE)

    def test_legacy_cooldown_survives_without_reopening_public_endpoint(self):
        old = "/api/quotes/simple"
        self.client.transport.restore({"venue":"Variational", "retry_at":NOW+90, "path":old,
                                      "next_quote_at":NOW+6, "quote_interval":6, "failures":{old:3}})
        state = self.client.transport.state()
        self.assertEqual(state["retry_at"], NOW + 90)
        self.assertEqual(state["next_quote_at"], NOW + 6)
        self.assertEqual(state["failures"], {"/api/quotes/indicative":3})
        current, status = self.cache.read()
        self.assertIsNone(current)
        self.assertIn("429", status["refresh_error"])
        self.assertEqual(self.opener.requests, [])
        with self.assertRaises(GridError):
            self.client.transport.request("POST", old)

    def test_credentials_cannot_be_bound_to_lighter_or_order_routes(self):
        for origin, routes in [("https://example.invalid", {("POST", "/api/quotes/indicative")}),
                               (ORIGIN, {("POST", "/api/quotes/accept")})]:
            with self.assertRaises(GridError):
                _Transport(origin, routes, session=self.client.session)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.invalid"))
        for path in ("/api/quotes/accept", "/api/orders/new/market", "/api/orders/cancel", "/api/me"):
            with self.assertRaises(GridError):
                self.client.transport.request("POST", path)
        self.assertEqual(self.opener.requests, [])


if __name__ == "__main__":
    unittest.main()
