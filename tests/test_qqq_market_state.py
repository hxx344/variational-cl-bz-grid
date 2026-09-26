"""Market closure, authentication and quote freshness are independent facts."""
from dataclasses import replace
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from variational_grid.client import save_session
from variational_grid.models import D
from variational_grid.qqq_comparison import QQQCohort, QQQExperiment, QQQMarketFeed, write_export
from variational_grid.qqq_market import VarSwapClient
from variational_grid.qqq_pricing import QQQPricing, ReferenceCache
from variational_grid.qqq_scalper import CURRENT_MODEL, ScalperSettings
from test_qqq import trade
from test_qqq_auth import token
from test_qqq_cache import QuoteSource
from test_qqq_execution import book
from test_qqq_market import NOW
from test_qqq_scalper import config


class Source(QuoteSource):
    get_failure = None

    def open(self, request, timeout):
        if request.method == "GET" and self.get_failure:
            raise urllib.error.HTTPError(request.full_url, self.get_failure, "fixture", {"Retry-After": "60"}, io.BytesIO())
        return super().open(request, timeout)


class MarketStateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root, self.now = Path(folder.name), [NOW]
        clock = patch("variational_grid.client.time.time", side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.session_path = self.root / "session.json"
        self.write_token()
        self.source = Source(self.now)
        self.client = VarSwapClient(session_file=self.session_path, opener=self.source,
                                    clock=lambda: self.now[0], max_age_seconds=60)
        self.cache_path = self.root / "quote-cache.json"
        self.cache = ReferenceCache(self.client, self.cache_path, QQQPricing(), write_export)

    def write_token(self, signature="first", expiry=NOW + 3600):
        save_session(self.session_path, {"token": token(expiry, signature)})

    def test_startup_closed_without_quote_is_pending_not_rejected(self):
        self.source.closed = True
        quote, status = self.cache.read()
        self.assertIsNone(quote)
        self.assertEqual((status["market_state"], status["authentication_state"]), ("closed", "pending"))
        self.assertEqual(status["refresh_error_kind"], "market_closed")
        self.assertEqual(status["authentication_error"], "")
        self.assertIsNone(status["source_ts"])
        self.assertEqual(self.source.posts, [])

    def test_rotation_and_restart_during_closure_preserve_old_quote_age(self):
        self.cache.read()
        self.now[0] += 31
        self.source.closed = True
        self.write_token("replacement")
        quote, status = self.cache.read()
        self.assertIsNone(quote)
        self.assertEqual((status["market_state"], status["authentication_state"]), ("closed", "pending"))
        self.assertEqual(status["source_ts"], NOW)
        self.assertEqual(status["market_source_ts"], NOW + 31)
        other = VarSwapClient(session_file=self.session_path, opener=self.source, clock=lambda: self.now[0])
        restored = ReferenceCache(other, self.cache_path, QQQPricing(), write_export)
        quote, status = restored.read()
        self.assertIsNone(quote)
        self.assertEqual(status["source_ts"], NOW)
        self.assertEqual(status["authentication_state"], "pending")
        self.assertEqual(len(self.source.posts), 1)

    def test_closed_market_does_not_hide_expired_or_rejected_session(self):
        self.source.closed = True
        self.cache.read()
        self.write_token(expiry=NOW + 20)
        quote, status = self.cache.read()
        self.assertIsNone(quote)
        self.assertEqual((status["market_state"], status["authentication_state"]), ("closed", "unavailable"))
        self.assertIn("过期", status["authentication_error"])
        self.write_token("valid-again")
        self.now[0] += 31
        self.source.get_failure = 401
        quote, status = self.cache.read()
        self.assertIsNone(quote)
        self.assertEqual((status["market_state"], status["authentication_state"]), ("closed", "rejected"))
        self.assertIn("401/403", status["authentication_error"])
        self.assertEqual(self.source.posts, [])

    def test_closure_and_rate_limit_coexist_then_metadata_expires_to_unknown(self):
        self.source.closed = True
        self.cache.read()
        self.now[0] += 31
        self.source.get_failure = 429
        quote, status = self.cache.read()
        self.assertIsNone(quote)
        self.assertEqual(status["market_state"], "closed")
        self.assertIn("429", status["refresh_error"])
        self.now[0] = NOW + 121
        _, status = self.cache.read()
        self.assertEqual(status["market_state"], "unknown")
        self.assertEqual(status["authentication_state"], "pending")
        self.assertEqual(self.source.posts, [])

    def test_opening_needs_a_new_authenticated_quote_before_becoming_available(self):
        self.cache.read()
        self.write_token("replacement")
        self.source.closed = True
        self.now[0] += 31
        self.cache.read()
        self.source.closed, self.source.failure = False, "network"
        self.now[0] += 31
        quote, status = self.cache.read()
        self.assertIsNone(quote)
        self.assertEqual((status["market_state"], status["authentication_state"]), ("open", "pending"))
        self.assertEqual(status["source_ts"], NOW)
        self.source.failure = None
        self.now[0] += 4
        quote, status = self.cache.read()
        self.assertIsNotNone(quote)
        self.assertEqual(status["authentication_state"], "confirmed")
        self.assertEqual(status["source_ts"], self.now[0])

    def test_final_frame_clock_does_not_extend_market_or_quote_lifetimes(self):
        self.cache.read()
        self.client._market["closes_at"] = NOW + 2
        quote, status = self.cache.status(NOW + 2, cache_used=True, error="")
        self.assertIsNone(quote)
        self.assertEqual(status["market_state"], "closed")
        _, status = self.cache.status(NOW + 121, cache_used=True, error="")
        self.assertEqual(status["market_state"], "unknown")
        self.assertEqual(status["source_ts"], NOW)
        self.client._market["metadata_ts"] = NOW + 5
        self.assertEqual(self.client.market_observation(NOW)["market_state"], "unknown")

    def test_close_only_is_separate_from_closed(self):
        self.source.close_only = True
        quote, status = self.cache.read()
        self.assertIsNotNone(quote)
        self.assertEqual(status["market_state"], "close_only")

    def test_closed_var_keeps_short_while_qqq_tp_and_inflight_entry_are_accounted(self):
        output = self.root / "paper"
        cfg = replace(config(), state_file=str(output / "a.sqlite3"), hedge_threshold_usdc="1",
                      scalper=ScalperSettings(model=CURRENT_MODEL))
        experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), output, {cfg.name: cfg}, cfg.settings,
                                   scalper=cfg.scalper)
        current_book = [book(NOW, "99.99", "100.01")]
        feed = QQQMarketFeed(experiment, SimpleNamespace(snapshot=lambda: current_book[0]), self.client)
        def advance(cohort, elapsed, trades=(), crossed=False):
            self.now[0] = NOW + elapsed
            current_book[0] = book(self.now[0], *(() if crossed else ("99.99", "100.01")), trades=trades)
            frame = feed.next(cohort)
            return frame, cohort.ingest(frame)["scenarios"][0]
        with QQQCohort(experiment) as cohort:
            advance(cohort, 0)
            _, opened = advance(cohort, 2, [trade(1, NOW + 1, "100", "2")])
            short = opened["us100"]["qty"]
            self.assertLess(D(short), 0)
            self.source.closed = True
            frame, closed = advance(cohort, 31, crossed=True)
            self.assertFalse(frame.market["allow_entries"])
            self.assertEqual(frame.market["quote_cache"]["market_state"], "closed")
            self.assertEqual(frame.quotes, {})
            self.assertEqual(D(closed["qqq"]["qty"]), 0)
            self.assertEqual(closed["us100"]["qty"], short)
            self.assertEqual(closed["hedge_status"], "var_market_unavailable")
            self.assertLess(D(closed["net_exposure_usdc"]), 0)
            _, inflight = advance(cohort, 33, [trade(2, NOW + 31.2, "100", "1"), trade(3, NOW + 31.4, "100", "7")])
            self.assertEqual(D(inflight["qqq"]["qty"]), 1)
            self.assertEqual(inflight["scalper"]["active_entries"], 0)
            _, exited = advance(cohort, 35, crossed=True)
            self.assertEqual(D(exited["qqq"]["qty"]), 0)
            self.assertEqual(exited["us100"]["qty"], short)
            self.assertEqual(len(self.source.posts), 1)
            self.source.closed = False
            frame, reopened = advance(cohort, 62)
            self.assertTrue(frame.market["allow_entries"])
            # The existing dollar-band strategy targets half the threshold,
            # rather than guaranteeing an exactly flat residual position.
            self.assertLess(abs(D(reopened["us100"]["qty"])), abs(D(short)))
            self.assertLessEqual(abs(D(reopened["net_exposure_usdc"])), D(cfg.hedge_threshold_usdc))
            self.assertGreater(reopened["hedge_adjustments"], exited["hedge_adjustments"])


if __name__ == "__main__":
    unittest.main()
