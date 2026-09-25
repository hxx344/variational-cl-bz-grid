import copy
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from variational_grid.models import Config, D, GridError
from variational_grid.qqq_comparison import QQQExperiment, QQQCohort, QQQMarketFeed, read_qqq_dashboard, write_export
from variational_grid.qqq_hedge import QQQConfig, QQQSettings
from variational_grid.qqq_pricing import QQQPricing, ReferenceCache
from variational_grid.reset import read_state, request_reset, process_reset
from test_qqq import market, trade
from test_qqq_market import NOW, Response, var_metadata, var_quote, VarSwapClient


class QuoteSource:
    def __init__(self, now):
        self.now, self.posts, self.gets = now, [], 0
        self.failure, self.lag, self.closed, self.close_only = None, 0, False, False

    def open(self, request, timeout):
        if request.method == "GET":
            self.gets += 1
            data = var_metadata()
            data["US100S"][0].update(market_status="closed" if self.closed else "open", is_close_only_mode=self.close_only)
            return Response(data, self.now[0])
        self.posts.append((self.now[0], json.loads(request.data)["qty"]))
        if self.failure == 429:
            raise urllib.error.HTTPError(request.full_url, 429, "limited", {"Retry-After": "90"}, io.BytesIO())
        if self.failure:
            raise OSError("Unavailable")
        return Response(var_quote(now=self.now[0] - self.lag), self.now[0])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "quote-cache.json"
        self.now = [NOW]
        self.source = QuoteSource(self.now)
        self.client = VarSwapClient(opener=self.source, clock=lambda: self.now[0], max_age_seconds=60)
        self.cache = ReferenceCache(self.client, self.path, QQQPricing(), write_export)

    def test_source_age_three_seconds_hits_then_refreshes_and_writes_only_changes(self):
        first, _ = self.cache.read()
        persisted = self.path.read_bytes()
        with patch.object(self.cache, "writer", wraps=write_export) as writer:
            for age in (1, 2, 3):
                self.now[0] = NOW + age
                current, state = self.cache.read()
                self.assertEqual(current["ts"], first["ts"])
                self.assertTrue(state["cache_used"])
            self.assertEqual(len(self.source.posts), 1)
            writer.assert_not_called()
        self.now[0] = NOW + 3.1
        current, state = self.cache.read()
        self.assertFalse(state["cache_used"])
        self.assertEqual(current["ts"], self.now[0])
        self.assertNotEqual(self.path.read_bytes(), persisted)
        self.assertEqual(len(self.source.posts), 2)

    def test_source_lag_does_not_get_three_extra_seconds_from_receipt(self):
        self.source.lag = 4
        current, _ = self.cache.read()
        self.assertEqual(current["ts"], NOW - 4)
        self.now[0] += 1
        current, state = self.cache.read()
        self.assertTrue(state["cache_used"])
        self.assertEqual(state["age_seconds"], 5)
        self.assertIn("排队", state["refresh_error"])
        self.assertEqual(len(self.source.posts), 1)

    def test_older_refresh_preserves_fresher_cache_and_its_expiry(self):
        self.cache.read()
        self.now[0] += 4
        self.source.lag = 54
        current, state = self.cache.read()
        self.assertEqual(len(self.source.posts), 2)
        self.assertEqual(current["ts"], NOW)
        self.assertTrue(state["cache_used"])
        self.assertEqual(state["age_seconds"], 4)
        self.assertEqual(json.loads(self.path.read_text())["quote"]["ts"], NOW)
        self.source.failure = "network"
        self.now[0] = NOW + 20
        current, state = self.cache.read()
        self.assertTrue(state["available"])
        self.assertEqual(current["ts"], NOW)

    def test_rate_limit_fallback_expires_at_original_age_and_restart_keeps_time(self):
        self.cache.read()
        self.now[0] += 4
        self.source.failure = 429
        current, state = self.cache.read()
        self.assertTrue(state["available"])
        self.assertIn("429", state["refresh_error"])
        saved = self.client.transport.state()
        other = VarSwapClient(opener=self.source, clock=lambda: self.now[0], max_age_seconds=60)
        other.transport.restore(saved)
        recovered = ReferenceCache(other, self.path, QQQPricing(), write_export)
        for age, expected in ((20, False), (60, False), (60.1, False)):
            self.now[0] = NOW + age
            current, state = recovered.read()
            self.assertEqual(current is not None, expected)
            self.assertEqual(state["source_ts"], NOW)
        self.assertEqual(len(self.source.posts), 2)
        self.assertEqual(self.source.gets, 1)

    def test_corrupt_cache_is_ignored_and_valid_source_replaces_it(self):
        self.path.write_text('{"version":1,"quote":{"bid":"NaN"},"market":{}}')
        cache = ReferenceCache(self.client, self.path, QQQPricing(), write_export)
        self.assertIsNone(cache.quote)
        current, state = cache.read()
        self.assertTrue(state["available"])
        self.assertEqual(current["symbol"], "US100S")

    def test_closed_or_expired_metadata_never_revived_by_cached_quote(self):
        self.cache.read()
        self.source.closed = True
        self.now[0] += 31
        current, _ = self.cache.read()
        self.assertIsNone(current)
        restored = ReferenceCache(VarSwapClient(opener=self.source, clock=lambda: self.now[0]), self.path, QQQPricing(), write_export)
        self.assertIsNone(restored.usable(self.now[0]))
        # Even a 300-second quote policy cannot extend the metadata TTL.
        self.source.closed = False
        self.now[0] = NOW + 70
        self.cache.read()
        self.cache.policy = QQQPricing(max_age_seconds=300)
        self.assertIsNone(self.cache.usable(NOW + 191))
        self.assertIsNone(self.cache.usable(self.client._market["closes_at"]))

    def test_known_close_only_state_is_inherited_by_cached_reference(self):
        self.cache.read()
        self.source.close_only = True
        self.source.failure = "network"
        self.now[0] += 31
        current, state = self.cache.read()
        self.assertTrue(current["close_only"])
        self.assertTrue(state["cache_used"])


class SharedPricingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = [NOW]
        settings = QQQSettings(grid_count=2)
        output = self.root / "paper"
        configs = {}
        for spacing in (".05", ".1", ".2"):
            for band in ("0", "2", "5"):
                name = f"s{spacing}-h{band}"
                configs[name] = QQQConfig(settings, name, spacing, band, str(output / "ledgers" / (name + ".sqlite3")))
        self.experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), output, configs, settings)
        self.source = QuoteSource(self.now)
        self.var = VarSwapClient(opener=self.source, clock=lambda: self.now[0], max_age_seconds=60)
        self.q = [market(NOW)]
        self.lighter = SimpleNamespace(snapshot=lambda: self.q[0])
        self.feed = QQQMarketFeed(self.experiment, self.lighter, self.var)

    def next(self, cohort, elapsed, trades=(), mark="100"):
        self.now[0] = NOW + elapsed
        self.q[0] = market(self.now[0], trades, mark)
        with patch("variational_grid.qqq_comparison.time.time", side_effect=lambda: self.now[0]):
            return self.feed.next(cohort)

    def test_nine_different_sizes_hedge_from_one_quote_and_record_true_fill_time(self):
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.next(cohort, 0))
            observation = self.next(cohort, 2, [trade(1, NOW + 1, price="95", qty="1000")])
            self.assertGreater(len(observation.quotes), 3)
            result = cohort.ingest(observation)
            self.assertTrue(all(D(row["us100"]["qty"]) < 0 for row in result["scenarios"]))
            self.assertEqual(len(self.source.posts), 1)
            for store in cohort.stores.values():
                fills = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM fills")]
                fill = next(r for r in fills if r["venue"] == "Variational")
                self.assertEqual((fill["ts"], fill["quote_ts"], fill["source_qty"]), (NOW + 2, NOW, "0.01"))
                self.assertTrue(fill["cache_used"])
            self.source.failure = 429
            for elapsed, price in ((4, "100.01"), (6, "100.02"), (8, "100.03")):
                frame = self.next(cohort, elapsed, mark=price)
                self.assertTrue(frame.market["allow_entries"])
                result = cohort.ingest(frame)
            self.assertEqual(len(self.source.posts), 2)  # One initial price plus one failed refresh, not nine per tick.
            self.assertTrue(all(r["pricing_stats"]["cached_price_fills"] >= 1 for r in result["scenarios"]))
            self.assertEqual(result["market"]["quote_cache"]["age_seconds"], 8)
            expired = self.next(cohort, 61)
            self.assertIsNone(expired.market["var"])
            self.assertFalse(expired.market["allow_entries"])
            cohort.ingest(expired)

    def test_shared_quote_validation_rejects_price_mutation_and_preserves_qqq_age_limit(self):
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.next(cohort, 0))
            frame = self.next(cohort, 2, [trade(1, NOW + 1, price="95", qty="1000")])
            tampered = copy.deepcopy(frame)
            next(iter(tampered.quotes.values()))["bid"] = "30000"
            with self.assertRaises(GridError):
                tampered.validate(self.experiment)

            tampered = copy.deepcopy(frame)
            tampered.market["lighter"]["ts"] -= 20
            with self.assertRaises(GridError):
                tampered.validate(self.experiment)

    def test_fixed_half_spread_is_015_bps_without_extra_one_bps(self):
        settings = replace(self.experiment.settings, var_slippage_bps="0")
        self.experiment.settings = settings
        self.experiment.pricing = QQQPricing(half_spread_percent="0.0015")
        self.experiment.scenarios = {name: replace(c, settings=settings, hedge_tolerance_percent=None,
                                    hedge_threshold_usdc="3000", half_spread_percent="0.0015") for name, c in self.experiment.scenarios.items()}
        self.feed = QQQMarketFeed(self.experiment, self.lighter, self.var)
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.next(cohort, 0))
            frame = self.next(cohort, 2, [trade(1, NOW + 1, price="95", qty="1000")], mark="200")
            result = cohort.ingest(frame)
            self.assertEqual(D(frame.market["var"]["bid"]), D("30500") * D("0.999985"))
            self.assertEqual(D(frame.market["var"]["ask"]), D("30500") * D("1.000015"))
            self.assertTrue(all(abs(D(row["net_exposure_usdc"]) - 1500) < D(".04") for row in result["scenarios"]))
            self.assertTrue(all(not row["hedge_pending"] for row in result["scenarios"]))
            for store in cohort.stores.values():
                fills = [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM fills")]
                fill = next(row for row in fills if row["venue"] == "Variational")
                self.assertEqual(D(fill["price"]), D("30500") * D("0.999985"))
                self.assertEqual(fill["half_spread_percent"], "0.0015")
                self.assertEqual(D(fill["source_bid"]), D("30499.5"))
            history = read_qqq_dashboard(self.experiment, "24h")["history"]["points"]
            self.assertTrue(all(abs(value - 1500) < .04 for value in history[-1]["net_exposure"]))

    def test_half_spread_is_economic_but_cache_age_is_not(self):
        config = next(iter(self.experiment.scenarios.values()))
        original = json.loads(config.strategy_identity())
        self.assertNotIn("half_spread_percent", original)
        self.assertNotIn("hedge_threshold_usdc", original)
        self.assertNotEqual(config.strategy_identity(), replace(config, half_spread_percent="0.0015").strategy_identity())

    def test_new_pricing_crash_replay_is_exactly_once_and_reset_keeps_cache(self):
        identity = self.experiment.identity()
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.next(cohort, 0))
            frame = self.next(cohort, 2, [trade(1, NOW + 1, price="95", qty="1000")])
            victim = list(cohort.engines.values())[1]
            with patch.object(victim, "apply", side_effect=RuntimeError("power loss")):
                with self.assertRaises(RuntimeError):
                    cohort.ingest(frame)
        with QQQCohort(self.experiment) as cohort:
            rows = cohort.latest()["scenarios"]
            self.assertTrue(all(r["pricing_stats"]["reference_price_fills"] == 1 for r in rows))
            before = cohort.latest()
            cohort.recover()
            self.assertEqual(before, cohort.latest())
            request_reset(self.experiment, read_state(self.experiment)["generation"])
            self.assertTrue(process_reset(cohort))
            self.assertTrue((self.experiment.output / "quote-cache.json").exists())
            restored = QQQMarketFeed(self.experiment, self.lighter, VarSwapClient(opener=self.source, clock=lambda: self.now[0], max_age_seconds=60))
            self.assertEqual(restored.reference.quote["ts"], NOW)
            self.assertIsNone(restored.reference.usable(self.now[0]))  # Restart needs an authenticated quote.
            self.assertEqual(self.experiment.identity(), identity)

    def test_existing_configuration_and_identity_survive_policy_changes(self):
        template = json.loads(Path("qqq-hedge.example.json").read_text())
        (self.root / "base.json").write_text(json.dumps(asdict(Config())))
        template.update(base_config="base.json", output_dir="old")
        template.pop("pricing")
        path = self.root / "qqq.json"
        path.write_text(json.dumps(template))
        old = QQQExperiment.load(path)
        self.assertEqual(old.pricing.refresh_after_seconds, 3)
        identity = old.identity()
        with QQQCohort(old):
            pass
        template["pricing"] = {"max_age_seconds": 90}
        path.write_text(json.dumps(template))
        upgraded = QQQExperiment.load(path)
        with QQQCohort(upgraded):
            pass
        self.assertEqual(upgraded.identity(), identity)


if __name__ == "__main__":
    unittest.main()
