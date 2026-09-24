import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from variational_grid.comparison import Experiment
from variational_grid.inventory import (InventoryConfig, InventorySettings, empty_signals, exposure,
                                        project_inventory, quote_key, signal_step)
from variational_grid.inventory_comparison import (InventoryCohort, InventoryExperiment, InventoryFrame,
                                                   InventoryMarketFeed, common_step, read_inventory_dashboard,
                                                   synthetic_quote, execution_quantum)
from variational_grid.models import Config, D, GridError, utc
from variational_grid.reset import read_state, request_reset, process_reset


class InventoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = Config(grid_step_percent="1", max_levels=None, max_margin_fraction=None,
                           paper_leverage="100", fee_bps_per_leg="1", slippage_bps_per_leg="1")
        self.settings = InventorySettings(scalp_cooldown_seconds=0)
        self.start = 1735689600
        self.experiment = self.make_experiment()

    def make_experiment(self, limits=("0", "5", "10", "20", None), output="experiment"):
        output = self.root / output
        configs = {f"band-{i}": InventoryConfig(self.base, self.settings, f"band-{i}", limit,
                                               str(output / "ledgers" / f"band-{i}.sqlite3")) for i, limit in enumerate(limits)}
        return InventoryExperiment(self.base, output, configs, self.settings)

    def frame(self, cohort, i=0, cl="100", bz="104", raw=None, allow_open=True):
        now = self.start + i * 10
        marks = {"CL": cl, "BZ": bz}
        frame = cohort.prepare(marks, {"CL": "100", "BZ": "104", "spread": "4"}, now, allow_open)
        if raw is not None:
            frame.raw = {s: str(raw[s]) for s in ("CL", "BZ")}
            frame.plans = {name: engine.preview(frame.raw, frame.execution_step, allow_open) for name, engine in cohort.engines.items()}
        for key in cohort.required_quotes(frame) | {quote_key(s, 1) for s in ("CL", "BZ")}:
            symbol, quantity = key.split(":")
            frame.quotes[key] = synthetic_quote(symbol, D(quantity), marks[symbol], now)
        cohort.finalize(frame)
        return frame

    def assert_accounting(self, summary):
        for row in summary["scenarios"]:
            total = D(row["total_pnl_usdc"])
            self.assertLess(abs(total - D(row["realized_pnl_usdc"]) - D(row["unrealized_pnl_usdc"])), D("1e-20"))
            attributed = D(row["direction_pnl_usdc"]) + D(row["spread_pnl_usdc"]) - D(row["execution_cost_usdc"]) - D(row["exit_cost_reserve_usdc"])
            self.assertLess(abs(total - attributed), D("1e-20"))


class ProjectionTests(unittest.TestCase):
    def test_same_direction_correction_preserves_spread(self):
        raw = {"CL": "10", "BZ": "-6"}
        for limit in ("0", "5", "10", "20"):
            target, overlay, status = project_inventory(raw, "0", limit, ".01")
            self.assertEqual(exposure(target)[1], D("-8"))
            self.assertLessEqual(exposure(target)[2], D(limit) / 100)
            self.assertEqual(status, "adjusted")
            if limit == "0":
                self.assertEqual(target, {"CL": D(8), "BZ": D(-8)})
        target, overlay, status = project_inventory(raw, "-1", None, ".01")
        self.assertEqual(target, {"CL": D(10), "BZ": D(-6)})
        self.assertEqual(overlay, 0)

    def test_hysteresis_flat_and_negative_inventory(self):
        target, overlay, _ = project_inventory({"CL": "-6", "BZ": "10"}, 0, "10", ".01")
        self.assertEqual(exposure(target)[0], D("0.80"))
        repeated = project_inventory({"CL": "-6", "BZ": "10"}, overlay, "10", ".01")
        self.assertEqual(repeated, (target, overlay, "inside"))
        self.assertEqual(project_inventory({"CL": "2", "BZ": "2"}, 0, "20", ".01")[0], {"CL": D(0), "BZ": D(0)})
        self.assertEqual(project_inventory({"CL": "0", "BZ": "0"}, "3", "20", ".01")[1], 0)
        self.assertEqual(exposure({"CL": 0, "BZ": 0})[2], 0)

    def test_unattainable_zero_band_reports_residual(self):
        target, overlay, status = project_inventory({"CL": "2", "BZ": "-1"}, 0, "0", "1")
        self.assertEqual(status, "quantity_limited")
        self.assertEqual(abs(exposure(target)[0]), 1)
        self.assertEqual(exposure(target)[1], D("-1.5"))
        self.assertEqual(overlay % 1, 0)

    def test_quantity_lcm_is_exact(self):
        self.assertEqual(common_step(["0.02", "0.03", "0.01"]), D("0.06"))
        with self.assertRaises(GridError):
            common_step(["0"])
        self.assertEqual(execution_quantum("1", ".01", ".03"), D(".05"))
        self.assertEqual(execution_quantum("1", ".01", ".3"), D(".5"))
        self.assertEqual(execution_quantum("1", ".01", ".7"), D("1"))


class SignalTests(InventoryFixture):
    def test_common_downtrend_accumulates_asymmetric_scalp_inventory(self):
        settings = replace(self.settings, enable_ordinary=False, enable_spread=False)
        config = replace(next(iter(self.experiment.scenarios.values())), settings=settings)
        state = empty_signals()
        centers = {"CL": "100", "BZ": "104", "spread": "4"}
        state, raw, _, _ = signal_step(state, {"CL": "100", "BZ": "104"}, centers, self.start, config)
        self.assertEqual(raw, {"CL": "1", "BZ": "-1"})
        state, raw, _, events = signal_step(state, {"CL": "99.8", "BZ": "103.8"}, centers, self.start + 10, config)
        self.assertEqual(raw, {"CL": "2", "BZ": "0"})
        self.assertTrue(any(e.get("reason") == "price_target" for e in events))

    def test_spread_is_reverse_long_only_and_tickets_expire(self):
        config = replace(next(iter(self.experiment.scenarios.values())), settings=replace(self.settings, enable_scalp=False, enable_ordinary=False))
        centers = {"CL": "100", "BZ": "104", "spread": "4"}
        state, raw, _, _ = signal_step(empty_signals(), {"CL": "100", "BZ": "104.1"}, centers, self.start, config)
        self.assertEqual(raw, {"CL": "0", "BZ": "0"})
        state, raw, _, _ = signal_step(state, {"CL": "100", "BZ": "103.9"}, centers, self.start + 10, config)
        self.assertEqual(raw, {"CL": "-1", "BZ": "1"})
        state, raw, _, events = signal_step(state, {"CL": "100", "BZ": "103.8"}, centers,
                                            self.start + 10 + config.max_holding_hours * 3600, config, False)
        self.assertFalse(state["lots"])
        self.assertEqual(events[0]["reason"], "max_holding")


class AccountTests(InventoryFixture):
    def test_thresholds_change_trades_and_equity_with_identical_raw_inventory(self):
        with InventoryCohort(self.experiment) as cohort:
            first = cohort.ingest(self.frame(cohort, raw={"CL": "10", "BZ": "-6"}))
            second = cohort.ingest(self.frame(cohort, 1, "99", "103", {"CL": "10", "BZ": "-6"}))
            self.assert_accounting(first)
            self.assert_accounting(second)
            self.assertEqual(D(second["scenarios"][0]["directional_barrels"]), 0)
            self.assertEqual(D(second["scenarios"][-1]["directional_barrels"]), 4)
            self.assertEqual(len({r["total_pnl_usdc"] for r in second["scenarios"]}), 5)
            for row in second["scenarios"]:
                self.assertEqual(row["raw_directional_barrels"], "4")
                self.assertEqual(D(row["spread_barrels"]), -8)
                self.assertGreater(D(row["execution_cost_usdc"]), 0)
                self.assertGreater(D(row["exit_cost_reserve_usdc"]), 0)

    def test_shadow_lots_are_not_external_volume(self):
        with InventoryCohort(self.experiment) as cohort:
            frame = self.frame(cohort, raw={"CL": "0", "BZ": "0"})
            self.assertTrue(frame.signals["lots"])
            summary = cohort.ingest(frame)
            for row in summary["scenarios"]:
                self.assertEqual(row["fill_count"], 0)
                self.assertEqual(D(row["volume_barrels"]), 0)
                self.assertEqual(D(row["fees_usdc"]), 0)
                self.assertEqual(D(row["total_pnl_usdc"]), 0)

    def test_reversal_cost_basis_and_identity(self):
        experiment = self.make_experiment((None,), "reversal")
        with InventoryCohort(experiment) as cohort:
            cohort.ingest(self.frame(cohort, raw={"CL": "2", "BZ": "0"}))
            frame = self.frame(cohort, 1, "110", "114", {"CL": "-1", "BZ": "0"})
            summary = cohort.ingest(frame)
            row = summary["scenarios"][0]
            account = cohort.stores["band-0"].account()
            self.assertEqual(account["positions"]["CL"]["qty"], "-1")
            self.assertEqual(D(account["positions"]["CL"]["average_price"]), D(frame.quotes["CL:3"]["bid"]) * D(".9999"))
            self.assertEqual(D(row["volume_barrels"]), 5)
            self.assertEqual(row["fill_count"], 2)
            self.assertGreater(D(row["realized_pnl_usdc"]), 19)
            self.assert_accounting(summary)

    def test_size_specific_quotes_missing_or_stale_change_no_accounts(self):
        with InventoryCohort(self.experiment) as cohort:
            frame = self.frame(cohort, raw={"CL": "10", "BZ": "-6"})
            missing = copy.deepcopy(frame)
            missing.quotes.pop("CL:10")
            with self.assertRaises(GridError):
                cohort.ingest(missing)
            stale = copy.deepcopy(frame)
            stale.quotes["CL:10"]["ts"] -= 100
            with self.assertRaises(GridError):
                cohort.ingest(stale)
            self.assertIsNone(cohort.latest())
            self.assertTrue(all(s.get("last_tick") is None for s in cohort.stores.values()))

    def test_close_only_never_increases_or_reverses_net_account(self):
        with InventoryCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort, raw={"CL": "10", "BZ": "-6"}))
            old = {n: s.account() for n, s in cohort.stores.items()}
            summary = cohort.ingest(self.frame(cohort, 1, raw={"CL": "-12", "BZ": "15"}, allow_open=False))
            for row in summary["scenarios"]:
                for symbol, key in (("CL", "cl_barrels"), ("BZ", "bz_barrels")):
                    before, after = D(old[row["name"]]["positions"][symbol]["qty"]), D(row[key])
                    self.assertLessEqual(abs(after), abs(before))
                    self.assertGreaterEqual(before * after, 0)

    def test_drawdown_closes_actual_net_account_and_latches(self):
        self.base = replace(self.base, paper_balance_usdc="100")
        experiment = self.make_experiment((None,), "halt")
        with InventoryCohort(experiment) as cohort:
            cohort.ingest(self.frame(cohort, raw={"CL": "10", "BZ": "0"}))
            second = cohort.ingest(self.frame(cohort, 1, "90", "94", {"CL": "10", "BZ": "0"}))
            self.assertEqual(second["scenarios"][0]["halted"], "max_drawdown")
            self.assertEqual(D(second["scenarios"][0]["cl_barrels"]), 0)
            third = cohort.ingest(self.frame(cohort, 2, raw={"CL": "100", "BZ": "-100"}))
            self.assertEqual(third["scenarios"][0]["fill_count"], second["scenarios"][0]["fill_count"])
            self.assert_accounting(third)

    def test_observed_exposure_excludes_unobserved_long_gap(self):
        experiment = self.make_experiment((None,), "gap")
        with InventoryCohort(experiment) as cohort:
            cohort.ingest(self.frame(cohort, raw={"CL": "4", "BZ": "0"}))
            cohort.ingest(self.frame(cohort, 1, raw={"CL": "4", "BZ": "0"}))
            row = cohort.ingest(self.frame(cohort, 100, raw={"CL": "4", "BZ": "0"}))["scenarios"][0]
            self.assertEqual(row["observed_seconds"], 10)
            self.assertEqual(row["exposure_seconds"], 10)
            self.assertEqual(D(row["mean_abs_directional_barrels"]), 4)


class PersistenceTests(InventoryFixture):
    def test_synthetic_and_live_sources_cannot_mix(self):
        with InventoryCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort))
            live = self.frame(cohort, 1)
            live.data_kind = "live_indicative"
            with self.assertRaises(GridError):
                cohort.ingest(live)
            self.assertEqual(cohort.latest()["sample_count"], 1)

    def test_partial_commit_replays_without_quotes_or_duplicate_volume(self):
        with InventoryCohort(self.experiment) as cohort:
            frame = self.frame(cohort, raw={"CL": "10", "BZ": "-6"})
            with patch.object(cohort.engines["band-1"], "apply", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    cohort.ingest(frame)
            first = cohort.stores["band-0"].snapshot()
            self.assertIsNone(cohort.latest())
        with patch.object(InventoryMarketFeed, "quote", side_effect=AssertionError("network on recovery")):
            with InventoryCohort(self.experiment) as cohort:
                summary = cohort.latest()
                self.assertEqual(summary["sample_count"], 1)
                self.assertEqual(summary["scenarios"][0], first)
                self.assertTrue(all(r["fill_count"] == 2 for r in summary["scenarios"]))
                self.assert_accounting(summary)

    def test_all_accounts_applied_before_publish_recovery(self):
        with InventoryCohort(self.experiment) as cohort:
            frame = self.frame(cohort)
            with cohort.db:
                cohort.db.execute("INSERT INTO frames VALUES (?,?)", (frame.ts, frame.encode()))
            for name, engine in cohort.engines.items():
                engine.apply(frame, frame.plans[name])
        with InventoryCohort(self.experiment) as cohort:
            self.assertEqual(cohort.latest()["sample_count"], 1)
            self.assertTrue(all(r["fill_count"] == 2 for r in cohort.latest()["scenarios"]))

    def test_changed_state_plan_rejected_before_journal(self):
        with InventoryCohort(self.experiment) as cohort:
            frame = self.frame(cohort)
            frame.plans["band-0"]["before"] = "a" * 64
            with self.assertRaises(GridError):
                cohort.ingest(frame)
            self.assertEqual(cohort.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 0)

    def test_reset_archives_accounts_and_shared_signal_state(self):
        with InventoryCohort(self.experiment) as cohort:
            cohort.ingest(self.frame(cohort))
            state = read_state(self.experiment)
            request_reset(self.experiment, state["generation"])
            self.assertTrue(process_reset(cohort))
            self.assertIsNone(cohort.latest())
            self.assertEqual(cohort.signal_state(), empty_signals())
            self.assertTrue(all(s.account()["fill_count"] == 0 for s in cohort.stores.values()))
            archive = self.experiment.output / "archives" / read_state(self.experiment)["archive_id"]
            self.assertTrue((archive / "complete.json").is_file())
            self.assertTrue((archive / "ledgers/band-0.sqlite3").is_file())

    def test_config_kind_and_identity_are_isolated(self):
        basepath = self.root / "base.json"
        basepath.write_text(json.dumps(asdict(self.base)), encoding="utf-8")
        path = self.root / "inventory.json"
        data = {"kind": "inventory", "base_config": "base.json", "output_dir": "saved",
                "strategy": asdict(self.settings), "scenarios": [{"name": "neutral", "tolerance_percent": "0"}, {"name": "free", "tolerance_percent": None}]}
        path.write_text(json.dumps(data), encoding="utf-8")
        experiment = Experiment.load(path)
        self.assertIsInstance(experiment, InventoryExperiment)
        with InventoryCohort(experiment):
            pass
        data["scenarios"][0]["tolerance_percent"] = "5"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(GridError):
            with InventoryCohort(Experiment.load(path)):
                pass

    def test_dashboard_uses_published_positions_not_ahead_account(self):
        with InventoryCohort(self.experiment) as cohort:
            first = cohort.ingest(self.frame(cohort))
            second = self.frame(cohort, 1, raw={"CL": "10", "BZ": "-6"})
            with cohort.db:
                cohort.db.execute("INSERT INTO frames VALUES (?,?)", (second.ts, second.encode()))
            cohort.engines["band-0"].apply(second, second.plans["band-0"])
            data = read_inventory_dashboard(self.experiment, "24h")
            self.assertEqual(data["summary"]["ts"], first["ts"])
            self.assertTrue(all(p["valued_at"] == first["ts"] for p in data["positions"]))
            self.assertTrue(all(t["ts"] <= first["ts"] for t in data["trades"]))


class LiveFeedTests(InventoryFixture):
    def client(self):
        now = self.start
        class FakeClient:
            def __init__(self):
                self.calls = []
                self.candle_calls = []
            def candles(self, symbol, hour, hours):
                self.candle_calls.append((symbol, hour, hours))
                return [{"unix_time_ms": t * 1000, "close": "100" if symbol == "CL" else "104"}
                        for t in range(hour - hours * 3600, hour, 3600)]
            def market(self, symbol):
                return True, False
            def request(self, method, path, *, body):
                if (method, path) != ("POST", "/quotes/indicative"):
                    raise AssertionError("Unexpected network route")
                symbol, qty = body["instrument"]["underlying"], D(body["qty"])
                self.calls.append(quote_key(symbol, qty))
                mark = D("98") if symbol == "CL" else D("102")
                quote = synthetic_quote(symbol, qty, mark, now)
                return {"instrument": body["instrument"], "qty": str(qty), "bid": quote["bid"], "ask": quote["ask"],
                        "mark_price": str(mark), "timestamp": utc(now),
                        "qty_limits": {side: {"min_qty_tick": ".001", "min_qty": ".002", "max_qty": "1000"} for side in ("bid", "ask")}}
        return FakeClient()

    def test_live_feed_deduplicates_all_dynamic_quantities_and_caches_history(self):
        client = self.client()
        feed = InventoryMarketFeed(self.experiment, client)
        with InventoryCohort(self.experiment) as cohort, patch("variational_grid.inventory_comparison.time.time", return_value=self.start):
            frame = feed.next(cohort)
            required = cohort.required_quotes(frame) | {"CL:1", "BZ:1"}
            self.assertEqual(set(client.calls), required)
            self.assertEqual(len(client.calls), len(required))
            self.assertTrue(any(D(k.split(":")[1]) != 1 for k in required))
            cohort.ingest(frame)
            client.calls.clear()
            feed.next(cohort)
            self.assertEqual(len(client.candle_calls), 2)

    def test_missing_dynamic_quote_does_not_advance_signals_or_accounts(self):
        client = self.client()
        original = client.request
        def failure(method, path, *, body):
            if D(body["qty"]) != 1:
                raise GridError("Quantity quote unavailable")
            return original(method, path, body=body)
        client.request = failure
        with InventoryCohort(self.experiment) as cohort, patch("variational_grid.inventory_comparison.time.time", return_value=self.start):
            with self.assertRaises(GridError):
                InventoryMarketFeed(self.experiment, client).next(cohort)
            self.assertEqual(cohort.signal_state(), empty_signals())
            self.assertTrue(all(s.get("last_tick") is None for s in cohort.stores.values()))

    def test_venue_minimum_is_not_treated_as_a_tick(self):
        client = self.client()
        original = client.request
        def minimum(method, path, *, body):
            result = original(method, path, body=body)
            for side in ("bid", "ask"):
                result["qty_limits"][side].update(min_qty=".03", min_qty_tick=".01")
            return result
        client.request = minimum
        with InventoryCohort(self.experiment) as cohort, patch("variational_grid.inventory_comparison.time.time", return_value=self.start):
            frame = InventoryMarketFeed(self.experiment, client).next(cohort)
            self.assertEqual(D(frame.execution_step), D(".05"))
            cohort.ingest(frame)
            self.assertEqual(cohort.latest()["sample_count"], 1)

    def test_crossing_hour_before_prepare_or_after_requests_rejects_frame(self):
        for timestamps in ([self.start + 3599, self.start + 3600],
                           [self.start + 3598, self.start + 3599, self.start + 3600]):
            experiment = self.make_experiment(output="boundary-" + str(len(timestamps)))
            with InventoryCohort(experiment) as cohort, patch("variational_grid.inventory_comparison.time.time", side_effect=timestamps):
                with self.assertRaises(GridError):
                    InventoryMarketFeed(experiment, self.client()).next(cohort)
                self.assertIsNone(cohort.latest())

    def test_expired_batch_is_not_recorded(self):
        with InventoryCohort(self.experiment) as cohort, patch("variational_grid.inventory_comparison.time.time", side_effect=[self.start, self.start, self.start + 16]):
            with self.assertRaisesRegex(GridError, "stale"):
                InventoryMarketFeed(self.experiment, self.client()).next(cohort)
            self.assertEqual(cohort.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
