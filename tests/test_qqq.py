import copy
from email.utils import formatdate
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

from variational_grid.comparison import Experiment
from variational_grid.models import D, Config, GridError
from variational_grid.qqq_hedge import QQQConfig, QQQSettings, book_fill, exposure, hedge_target, initial_account, maker_step
from variational_grid.qqq_comparison import QQQCohort, QQQExperiment, QQQFrame, QQQMarketFeed, read_qqq_dashboard
from variational_grid.reset import process_reset, read_state, request_reset
from variational_grid.qqq_market import LighterClient, VarSwapClient, RequestDeferred


def quote(ts, qty=".01", mark="30000"):
    return {"ts": ts, "qty": str(qty), "bid": str(D(mark) - D(".1")), "ask": str(D(mark) + D(".1")), "mark": mark,
            "size_step": ".000001", "min_qty": ".000004", "max_qty": "10000", "market_open": True}


def market(ts, trades=(), mark="100", bid=None, ask=None):
    return {"ts": ts, "bid": bid or str(D(mark) - D(".01")), "ask": ask or str(D(mark) + D(".01")), "mark": mark,
            "price_tick": ".01", "size_step": ".0001", "min_qty": ".0075", "min_notional": "10",
            "bids": [["50", "1"], ["99", "2"], ["99.99", "1"]], "asks": [["100.01", "1"], ["150", "1"]],
            "ready": True, "gap": False, "trades": list(trades)}


def trade(identity, ts, price="99", qty="3", side="sell"):
    return {"id": str(identity), "ts": ts, "price": price, "qty": qty, "side": side}


class BudgetedVar:
    """Deterministic prices behind the real HTTP request budget."""
    def __init__(self, now, fail=False, delay=0):
        self.now, self.fail, self.calls, self.delay = now, fail, [], delay
        self.transport = VarSwapClient(opener=self, clock=lambda: now[0]).transport

    def open(self, request, timeout):
        self.calls.append(json.loads(request.data)["qty"])
        if self.fail:
            raise urllib.error.HTTPError(request.full_url, 429, "limited", {"Retry-After": "90"}, io.BytesIO())
        self.now[0] += self.delay
        response = io.BytesIO(b"{}")
        response.headers = {"Date": formatdate(self.now[0], usegmt=True)}
        return response

    def market(self):
        self.transport.check_cooldown()
        return quote(self.now[0])

    def quote(self, qty):
        self.transport.request("POST", "/api/quotes/simple", body={"qty": str(qty)})
        return quote(self.now[0], qty)


class MechanicsTests(unittest.TestCase):
    def setUp(self):
        self.settings = QQQSettings(grid_count=2)
        self.config = QQQConfig(self.settings, "test", "1", "2", "unused")
        self.account, _ = maker_step(initial_account(), market(100), 100, self.config, True)

    def test_queue_partial_fill_and_no_same_batch_take_profit(self):
        # Two units ahead, trade three -> one unit filled. Later buy in the same
        # observed batch cannot hit a TP order submitted only after that batch.
        q = market(102, [trade(1, 101), trade(2, 101.5, "101", "20", "buy")], bid="98.99", ask="99.01")
        account, fills = maker_step(self.account, q, 102, self.config, True)
        self.assertEqual(account["qqq"]["qty"], "1")
        self.assertEqual(len(fills), 1)
        self.assertEqual(D(fills[0]["notional"]), 99)
        self.assertEqual(len([o for o in account["orders"] if o["side"] == "sell"]), 1)

    def test_trade_volume_is_not_reused_at_multiple_levels(self):
        q = market(102, [trade(1, 101, "97", "12")])
        account, fills = maker_step(self.account, q, 102, self.config, True)
        self.assertEqual(sum(D(f["qty"]) for f in fills), 12)
        self.assertEqual(D(account["qqq"]["qty"]), 12)
        self.assertEqual(len(fills), 2)

    def test_activation_latency_and_aggressor(self):
        q = market(101, [trade(1, 100.1, "97", "20"), trade(2, 100.5, "97", "20", "buy")])
        account, fills = maker_step(self.account, q, 101, self.config, True)
        self.assertEqual(fills, [])
        self.assertEqual(account["qqq"]["qty"], "0")

    def test_take_profit_partial_and_rearm(self):
        account, _ = maker_step(self.account, market(102, [trade(1, 101, "99", "3")], bid="98.99", ask="99.01"), 102, self.config, True)
        account, fills = maker_step(account, market(104, [trade(2, 103, "100", ".5", "buy")]), 104, self.config, True)
        self.assertEqual(D(account["qqq"]["qty"]), D(".5"))
        self.assertEqual(fills[0]["reason"], "maker_take_profit")
        self.assertGreater(D(account["qqq"]["realized_gross"]), 0)

    def test_depth_outside_visible_range_does_not_invent_queue_zero(self):
        q = market(100)
        q["bids"] = [["99.99", "2"]]
        account, _ = maker_step(initial_account(), q, 100, self.config, True)
        self.assertTrue(all(o["queue"] is None for o in account["orders"]))
        account, fills = maker_step(account, market(102, [trade(1, 101, "97", "100")]), 102, self.config, True)
        self.assertEqual(fills, [])
        self.assertTrue(all(o["active_ts"] == 102.2 for o in account["orders"]))

    def test_gap_retains_inventory_and_drops_uncertain_flow(self):
        account, _ = maker_step(self.account, market(102, [trade(1, 101)]), 102, self.config, True)
        q = market(104, [trade(2, 103, "97", "100")])
        q["gap"] = True
        after, fills = maker_step(account, q, 104, self.config, False)
        self.assertEqual(after["qqq"], account["qqq"])
        self.assertEqual(after["orders"], [])
        self.assertEqual(fills, [])

    def test_var_unavailable_still_accounts_inflight_maker_fills(self):
        account, fills = maker_step(self.account, market(102, [trade(1, 101)]), 102, self.config, False)
        self.assertEqual(len(fills), 1)
        buys = [o for o in account["orders"] if o["side"] == "buy"]
        self.assertTrue(all(o["cancel_ts"] == 102.3 for o in buys))
        account, fills = maker_step(account, market(104, [trade(2, 103, "97", "100")]), 104, self.config, False)
        self.assertEqual(fills, [])

    def test_half_band_solves_actual_gross_and_unwinds_when_qqq_flat(self):
        a = initial_account()
        a["qqq"]["qty"] = "10"
        for band in ("0", "2", "5"):
            config = replace(self.config, hedge_tolerance_percent=band)
            target = hedge_target(a, "100", "1000", config, ".000001")
            a["us100"]["qty"] = str(target)
            self.assertLess(abs(exposure(a, "100", "1000", "1")[2] - D(band) / 200), D(".000001"))
            a["us100"]["qty"] = "0"
        a["us100"]["qty"] = "-1"
        a["qqq"]["qty"] = "8"
        target = hedge_target(a, "100", "1000", self.config, ".000001")
        self.assertGreater(target, D("-1"))
        a["qqq"]["qty"] = "0"
        self.assertEqual(hedge_target(a, "100", "1000", self.config, ".000001"), 0)

    def test_two_sided_ledger_costs_and_turnover(self):
        a = initial_account()
        book_fill(a, "qqq", "2", "100", "1", 1, "entry")
        book_fill(a, "qqq", "-1", "103", "1", 2, "tp")
        book_fill(a, "us100", "-.01", "20000", "0", 1, "hedge")
        book_fill(a, "us100", ".005", "19800", "0", 2, "reduce")
        self.assertEqual(D(a["qqq"]["realized_gross"]), 3)
        self.assertEqual(D(a["us100"]["realized_gross"]), 1)
        self.assertEqual(D(a["qqq"]["turnover_usdc"]), 303)
        self.assertEqual(D(a["us100"]["turnover_usdc"]), 299)
        self.assertEqual(D(a["qqq"]["fees_usdc"]), D(".0303"))

    def test_anchor_uses_book_mid_and_full_take_profit_rearms_original_layers(self):
        q = market(100, mark="90")
        account, _ = maker_step(initial_account(), q, 100, self.config, True)
        self.assertEqual(D(account["anchor"]), 90)  # Default book follows mark.
        q.update(bid="99.99", ask="100.01")
        account, _ = maker_step(initial_account(), q, 100, self.config, True)
        self.assertEqual(D(account["anchor"]), 100)
        account, _ = maker_step(account, market(102, [trade(1, 101, "97", "100")], bid="97.99", ask="98.01"), 102, self.config, True)
        account, fills = maker_step(account, market(104, [trade(2, 103, "101", "100", "buy")], bid="99.49", ask="99.51"), 104, self.config, True)
        self.assertEqual(D(account["qqq"]["qty"]), 0)
        self.assertEqual(D(account["anchor"]), 100)
        self.assertEqual({D(o["price"]) for o in account["orders"]}, {D(99), D(98)})


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = QQQSettings(grid_count=2)
        self.output = self.root / "paper"
        configs = {f"band-{h}": QQQConfig(self.settings, f"band-{h}", "1", h, str(self.output / "ledgers" / (h + ".sqlite3"))) for h in ("0", "2", "5")}
        self.experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), self.output, configs, self.settings)

    def frame(self, cohort, ts, trades=(), var=True):
        m = {"lighter": market(ts, trades), "var": quote(ts) if var else None, "allow_entries": var, "reason": ""}
        frame = cohort.prepare_frame(ts, m, data_kind="synthetic")
        if var:
            for name, plan in frame.plans.items():
                amount = abs(D(plan["target"]) - D(cohort.stores[name].account()["us100"]["qty"]))
                if amount >= D(".000004"):
                    frame.quotes[format(amount.normalize(), "f")] = quote(ts, amount)
        return frame

    def test_isolated_hedges_same_qqq_fills_and_net_sum(self):
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            result = cohort.ingest(self.frame(cohort, 102, [trade(1, 101)]))
            self.assertEqual(len({r["qqq"]["qty"] for r in result["scenarios"]}), 1)
            self.assertEqual(len({r["us100"]["qty"] for r in result["scenarios"]}), 3)
            for row in result["scenarios"]:
                self.assertLess(abs(D(row["total_pnl_usdc"]) - D(row["qqq"]["total_pnl_usdc"]) - D(row["us100"]["total_pnl_usdc"])), D("1e-20"))
            dashboard = read_qqq_dashboard(self.experiment, "24h")
            self.assertEqual(len(dashboard["positions"]), 3)
            self.assertEqual(len(dashboard["trades"]), 6)

    def test_crash_after_first_account_recovers_exactly_once(self):
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            frame = self.frame(cohort, 102, [trade(1, 101)])
            victim = cohort.engines["band-2"]
            with patch.object(victim, "apply", side_effect=RuntimeError("power loss")):
                with self.assertRaises(RuntimeError):
                    cohort.ingest(frame)
        with QQQCohort(self.experiment) as cohort:
            self.assertEqual(cohort.latest()["sample_count"], 2)
            self.assertTrue(all(store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2 for store in cohort.stores.values()))
            before = cohort.latest()
            cohort.recover()
            self.assertEqual(cohort.latest(), before)

    def test_bad_quantity_or_plan_never_journaled(self):
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            frame = self.frame(cohort, 102, [trade(1, 101)])
            first = next(iter(frame.quotes.values()))
            first["qty"] = "1"
            with self.assertRaises(GridError):
                cohort.ingest(frame)
            self.assertEqual(cohort.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 1)

    def test_market_outage_does_not_erase_qqq_fills_and_uses_stale_valuation(self):
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            rows = cohort.ingest(self.frame(cohort, 102, [trade(1, 101)], var=False))["scenarios"]
            self.assertTrue(all(D(r["qqq"]["qty"]) == 1 and r["hedge_pending"] for r in rows))
            self.assertTrue(all(r["us100"]["qty"] == "0" for r in rows))
            rows = cohort.ingest(self.frame(cohort, 104))["scenarios"]
            self.assertTrue(all(D(r["us100"]["qty"]) < 0 for r in rows))

    def test_reset_archives_then_clears_all_ledgers(self):
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            cohort.ingest(self.frame(cohort, 102, [trade(1, 101)]))
            request_reset(self.experiment, read_state(self.experiment)["generation"])
            self.assertTrue(process_reset(cohort))
            self.assertIsNone(cohort.latest())
            self.assertTrue(all(s.account()["qqq"]["qty"] == "0" for s in cohort.stores.values()))
            archive = self.output / "archives" / read_state(self.experiment)["archive_id"]
            self.assertTrue((archive / "complete.json").is_file())

    def test_configuration_defaults_nine_and_independent_of_legacy_economics(self):
        template = json.loads(Path("qqq-hedge.example.json").read_text())
        (self.root / "base.json").write_text(json.dumps(asdict(Config())))
        template.update(base_config="base.json", output_dir="paper")
        path = self.root / "qqq.json"
        path.write_text(json.dumps(template))
        experiment = Experiment.load(path)
        self.assertEqual(len(experiment.scenarios), 9)
        identity = experiment.identity()
        (self.root / "base.json").write_text(json.dumps(asdict(Config(paper_leverage="100"))))
        self.assertEqual(Experiment.load(path).identity(), identity)

    def test_close_only_blocks_increasing_hedge_and_delayed_qqq_fills_survive(self):
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            frame = self.frame(cohort, 102, [trade(1, 101)])
            frame.market["var"]["close_only"] = True
            frame.market["allow_entries"] = False
            rows = cohort.ingest(frame)["scenarios"]
            self.assertTrue(all(r["hedge_status"] == "var_close_only" and r["us100"]["qty"] == "0" for r in rows))
            # HTTP delay must not discard previously observed maker fills.
            frame = self.frame(cohort, 104, [trade(2, 102.1, "98", "1")], var=False)
            frame.ts = 124
            frame = cohort.prepare_frame(frame.ts, frame.market, data_kind="synthetic")
            rows = cohort.ingest(frame)["scenarios"]
            self.assertTrue(all(D(r["qqq"]["qty"]) == 2 for r in rows))

    def test_public_feed_missing_hedge_quote_preserves_known_maker_execution(self):
        class Var:
            def __init__(self):
                self.calls = []
            def market(self):
                return quote(102)
            def quote(self, qty):
                self.calls.append(qty)
                raise GridError("Unavailable")
        var = Var()
        q = market(102, [trade(1, 101)])
        feed = QQQMarketFeed(self.experiment, SimpleNamespace(snapshot=lambda: q), var)
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            with patch("variational_grid.qqq_comparison.time.time", return_value=102):
                frame = feed.next(cohort)
            frame.data_kind = "synthetic"  # Injected test feed, never an actual public sample.
            result = cohort.ingest(frame)
            self.assertEqual(len(var.calls), 1)
            self.assertEqual(frame.market["reason"], "Unavailable")
            self.assertFalse(frame.market["allow_entries"])
            self.assertTrue(all(D(row["qqq"]["qty"]) == 1 and row["hedge_status"] == "hedge_quote_unavailable" for row in result["scenarios"]))

    def test_stale_lighter_cannot_rebalance_against_fresh_var(self):
        q = market(104, mark="105")
        q.update(ready=False, gap=True, reason="stale_http_source")
        var = SimpleNamespace(market=lambda: quote(104), quote=lambda qty: self.fail("Must not fetch execution quote"))
        feed = QQQMarketFeed(self.experiment, SimpleNamespace(snapshot=lambda: q), var)
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            prior = cohort.ingest(self.frame(cohort, 102, [trade(1, 101)]))
            with patch("variational_grid.qqq_comparison.time.time", return_value=104):
                frame = feed.next(cohort)
            frame.data_kind = "synthetic"
            self.assertIsNone(frame.market["var"])
            current = cohort.ingest(frame)
            self.assertEqual([r["us100"]["qty"] for r in current["scenarios"]], [r["us100"]["qty"] for r in prior["scenarios"]])

    def test_quote_budget_rotates_accounts_and_never_reuses_execution_price(self):
        now = [102]
        var = BudgetedVar(now)
        q = [market(102, [trade(1, 101)])]
        feed = QQQMarketFeed(self.experiment, SimpleNamespace(snapshot=lambda: q[0]), var)
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            for ts, expected in [(102, 1), (104, 1), (106, 2), (110, 3)]:
                now[0] = ts
                if ts != 102:
                    q[0] = market(ts)
                with patch("variational_grid.qqq_comparison.time.time", return_value=ts):
                    observation = feed.next(cohort)
                observation.data_kind = "synthetic"
                rows = cohort.ingest(observation)["scenarios"]
                self.assertEqual(sum(D(row["us100"]["qty"]) < 0 for row in rows), expected)
                self.assertEqual(len(var.calls), expected)
                self.assertTrue(all(D(row["qqq"]["qty"]) == 1 for row in rows))

    def test_429_during_hedge_stops_batch_retains_fills_and_survives_reset_restart(self):
        now = [102]
        var = BudgetedVar(now, fail=True)
        q = [market(102, [trade(1, 101)])]
        lighter = SimpleNamespace(snapshot=lambda: q[0])
        feed = QQQMarketFeed(self.experiment, lighter, var)
        identity = self.experiment.identity()
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            for ts in (102, 104, 106):
                now[0] = ts
                if ts != 102:
                    q[0] = market(ts)
                with patch("variational_grid.qqq_comparison.time.time", return_value=ts):
                    observation = feed.next(cohort)
                    dashboard = read_qqq_dashboard(self.experiment, "24h")
                self.assertEqual(dashboard["rate_limits"], [{"venue": "Variational", "retry_at": 192}])
                self.assertFalse(observation.market["allow_entries"])
                self.assertIsNone(observation.market["var"])
                self.assertEqual(observation.quotes, {})
                observation.data_kind = "synthetic"
                rows = cohort.ingest(observation)["scenarios"]
                self.assertTrue(all(D(row["qqq"]["qty"]) == 1 and row["us100"]["qty"] == "0" for row in rows))
            self.assertEqual(len(var.calls), 1)
            request_reset(self.experiment, read_state(self.experiment)["generation"])
            self.assertTrue(process_reset(cohort))
            replacement = BudgetedVar(now)
            restarted = QQQMarketFeed(self.experiment, lighter, replacement)
            with patch("variational_grid.qqq_comparison.time.time", return_value=108):
                observation = restarted.next(cohort)
            self.assertIsNone(observation.market["var"])
            self.assertEqual(replacement.calls, [])
            self.assertEqual(self.experiment.identity(), identity)

    def test_slow_exact_quote_does_not_drain_remaining_accounts_and_expire_the_frame(self):
        now = [102]
        var = BudgetedVar(now, delay=6)
        q = market(102, [trade(1, 101)])
        feed = QQQMarketFeed(self.experiment, SimpleNamespace(snapshot=lambda: q), var)
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            with patch("variational_grid.qqq_comparison.time.time", side_effect=lambda: now[0]):
                observation = feed.next(cohort)
            self.assertEqual(len(var.calls), 1)
            self.assertEqual(observation.ts, 108)
            observation.data_kind = "synthetic"
            rows = cohort.ingest(observation)["scenarios"]
            self.assertEqual(sum(D(row["us100"]["qty"]) < 0 for row in rows), 1)
            self.assertTrue(all(D(row["qqq"]["qty"]) == 1 for row in rows))

    def test_lighter_failure_saves_cooldown_without_publishing_frame(self):
        now = [102]
        var = BudgetedVar(now)
        def fail_lighter(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "limited", {"Retry-After": "100"}, io.BytesIO())
        lighter = LighterClient(opener=SimpleNamespace(open=fail_lighter), clock=lambda: now[0])
        feed = QQQMarketFeed(self.experiment, lighter, var)
        with QQQCohort(self.experiment) as cohort:
            with self.assertRaises(RequestDeferred):
                feed.next(cohort)
            self.assertIsNone(cohort.latest())
            self.assertEqual(json.loads((self.output / "market-cooldowns.json").read_text())["Lighter"]["retry_at"], 202)
            self.assertEqual(var.calls, [])

    def test_small_residual_explains_quantity_limit_instead_of_claiming_neutrality(self):
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            cohort.ingest(self.frame(cohort, 102, [trade(1, 101)]))
            row = cohort.ingest(self.frame(cohort, 104))["scenarios"][0]
            self.assertTrue(row["hedge_pending"])
            self.assertEqual(row["hedge_status"], "quantity_rounding_residual")

    def test_compact_history_keeps_all_samples_with_only_two_account_checkpoints(self):
        from variational_grid.qqq_comparison import decode_summary
        with QQQCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, 100))
            cohort.ingest(self.frame(cohort, 102, [trade(1, 101)]))
            for ts in (104, 106, 108):
                cohort.ingest(self.frame(cohort, ts))
            self.assertEqual(cohort.db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0], 5)
            for store in cohort.stores.values():
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0], 2)
            raw = cohort.db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()[0]
            self.assertEqual(decode_summary(raw), cohort.latest())
            view = read_qqq_dashboard(self.experiment, "24h")
            self.assertEqual(len(view["history"]["points"]), 5)
            self.assertEqual(len(view["trades"]), 6)
        with QQQCohort(self.experiment) as cohort:
            self.assertEqual(cohort.latest()["sample_count"], 5)

    def test_transient_windows_export_lock_retries_without_altering_ledger(self):
        from variational_grid.qqq_comparison import write_export
        path = self.root / "derived.html"
        replace_file = Path.replace
        calls = []
        def transient(source, target):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError("Temporary scanner lock")
            return replace_file(source, target)
        with patch("variational_grid.qqq_comparison.os", SimpleNamespace(name="nt")), patch.object(Path, "replace", transient), patch("variational_grid.qqq_comparison.time.sleep"):
            write_export(path, "saved")
        self.assertEqual(path.read_text(), "saved")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
