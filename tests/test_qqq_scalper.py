"""Sequential entry decisions, order lifetime and durable paper accounting."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from variational_grid.models import D, GridError
from variational_grid.qqq_comparison import QQQCohort, QQQExperiment
from variational_grid.qqq_hedge import QQQConfig, QQQSettings, initial_account, maker_step
from variational_grid.qqq_pricing import QQQPricing
from variational_grid.qqq_scalper import CURRENT_MODEL, ScalperSettings, candidate_prices, cooldown_seconds
from variational_grid.reset import process_reset, read_state, request_reset
from test_qqq import market, quote, trade


def config(**kwargs):
    return QQQConfig(QQQSettings(), "scalp", "0.05", None, "unused", "3000",
                     scalper=ScalperSettings(), take_profit_percent="0.05", **kwargs)


class ScalperTests(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.account, _ = maker_step(initial_account(), market(100), 100, self.config, True)

    def filled(self):
        return maker_step(self.account, market(102, [trade(1, 101, "100", "10")]), 102, self.config, True)[0]

    def test_single_near_book_entry_and_independent_tp_after_complete_fill(self):
        self.assertEqual(len(self.account["orders"]), 1)
        self.assertEqual(self.account["orders"][0]["price"], "100.00")
        self.assertEqual(self.account["slots"][0]["tp_price"], "100.05")
        q = market(102, [trade(1, 101, "100", "10"), trade(2, 101.5, "101", "100", "buy")])
        account, fills = maker_step(self.account, q, 102, self.config, True)
        self.assertEqual(len(fills), 1)  # The new TP cannot fill in the observation that creates it.
        self.assertEqual(fills[0]["maker_model"], "perp_dex_scalper_v1")
        self.assertEqual(D(account["qqq"]["qty"]), 10)
        self.assertEqual([o["side"] for o in account["orders"]], ["sell"])
        self.assertEqual(account["scalper"]["status"]["phase"], "cooling_down")

    def test_inventory_wait_boundaries_and_strict_elapsed_time(self):
        for count, expected in ((0, 112.5), (4, 112.5), (5, 225), (9, 225), (10, 450), (19, 450), (20, 900), (29, 900)):
            self.assertEqual(cooldown_seconds(count, 30, 450), expected)
        account = self.filled()
        account, _ = maker_step(account, market(214.5, mark="99"), 214.5, self.config, True)
        self.assertEqual(account["scalper"]["status"]["phase"], "cooling_down")
        account, _ = maker_step(account, market(214.6, mark="99"), 214.6, self.config, True)
        self.assertEqual(account["scalper"]["status"]["phase"], "opening")
        self.assertEqual(len([o for o in account["orders"] if o["side"] == "buy"]), 1)
        self.assertEqual(account["slots"][-1]["entry_price"], "99.00")

    def test_grid_gate_uses_ask_tp_and_strict_comparison(self):
        cfg = replace(self.config, grid_step_percent="1", take_profit_percent="2")
        q = market(100, bid="99.98", ask="100")
        boundary = D(100) * D("1.02") * D("1.01")
        self.assertFalse(candidate_prices(q, [boundary], cfg)[2])
        self.assertTrue(candidate_prices(q, [boundary + D(".000001")], cfg)[2])
        entry, target, _ = candidate_prices(q, [D("99.97")], cfg)
        self.assertEqual(entry, D("99.96"))
        self.assertEqual(target, D("101.95"))  # Original TP integer price truncation.

    def test_tp_completion_waives_one_round_and_reenters_at_current_price(self):
        account = self.filled()
        account, fills = maker_step(account, market(104, [trade(2, 103, "100.06", "10", "buy")], mark="100.10"), 104, self.config, True)
        self.assertEqual(fills[0]["reason"], "maker_take_profit")
        self.assertEqual(account["scalper"]["status"]["phase"], "opening")
        self.assertEqual(account["slots"][0]["entry_price"], "100.10")
        self.assertTrue(account["scalper"]["status"]["cooldown_waived"])
        self.assertEqual(account["slots"][0]["slot"], 2)
        self.assertEqual(D(account["qqq"]["turnover_usdc"]), D("2000.50"))

    def test_failed_price_gate_consumes_close_count_cooldown_waiver(self):
        account = self.filled()
        # Reconstruct a persisted decision before a previous batch closed.
        account["scalper"]["last_close_count"] = 2
        account, _ = maker_step(account, market(104, mark="100.02"), 104, self.config, True)
        self.assertEqual(account["scalper"]["status"]["phase"], "grid_blocked")
        account, _ = maker_step(account, market(106, mark="99"), 106, self.config, True)
        self.assertEqual(account["scalper"]["status"]["phase"], "cooling_down")
        self.assertFalse(account["scalper"]["status"]["cooldown_waived"])

    def test_partial_entry_waits_for_cancel_and_accounts_inflight_fills(self):
        account, _ = maker_step(self.account, market(102, [trade(1, 101, "100", "1")]), 102, self.config, True)
        self.assertFalse(any(o["side"] == "sell" for o in account["orders"]))
        account, _ = maker_step(account, market(119, mark="100.03"), 119, self.config, True)
        self.assertIsNone(account["orders"][0]["cancel_ts"])
        account, _ = maker_step(account, market(120, mark="100.03"), 120, self.config, True)
        self.assertEqual(account["orders"][0]["cancel_ts"], 120.3)
        q = market(122, [trade(2, 120.2, "100", "2"), trade(3, 120.4, "100", "7")], mark="100.03")
        account, fills = maker_step(account, q, 122, self.config, True)
        self.assertEqual(sum(D(f["qty"]) for f in fills), 2)
        self.assertEqual(D(account["qqq"]["qty"]), 3)
        self.assertEqual(account["scalper"]["last_entry_ts"], 122)
        self.assertEqual([o["side"] for o in account["orders"]], ["sell"])
        self.assertEqual(D(account["orders"][0]["remaining"]), 3)

    def test_reprice_only_up_after_twenty_seconds_then_five_second_polls(self):
        account, _ = maker_step(self.account, market(120, mark="99"), 120, self.config, True)
        self.assertIsNone(account["orders"][0]["cancel_ts"])
        account, _ = maker_step(account, market(124, mark="101"), 124, self.config, True)
        self.assertIsNone(account["orders"][0]["cancel_ts"])
        account, _ = maker_step(account, market(125, mark="101"), 125, self.config, True)
        self.assertEqual(account["orders"][0]["cancel_ts"], 125.3)
        account, _ = maker_step(account, market(127, mark="101"), 127, self.config, True)
        self.assertEqual(account["orders"][0]["price"], "101.00")
        self.assertEqual(account["qqq"]["qty"], "0")
        self.assertIsNone(account["scalper"]["last_entry_ts"])

    def test_one_tick_book_stays_post_only_without_cancel_churn(self):
        q = market(100, bid="100", ask="100.01")
        account, _ = maker_step(initial_account(), q, 100, self.config, True)
        self.assertEqual(D(account["orders"][0]["price"]), 100)
        account, _ = maker_step(account, {**q, "ts": 120}, 120, self.config, True)
        self.assertIsNone(account["orders"][0]["cancel_ts"])

    def test_maker_latency_queue_partial_tp_and_no_touch_fill(self):
        account, fills = maker_step(self.account, market(101, [trade(1, 100.1, "99", "100")], mark="99"), 101, self.config, True)
        self.assertEqual(fills, [])
        account = self.filled()
        account, fills = maker_step(account, market(104, [trade(2, 103, "100.05", "2", "buy")]), 104, self.config, True)
        self.assertEqual(D(account["qqq"]["qty"]), 8)
        self.assertEqual(account["scalper"]["last_close_count"], 1)
        self.assertEqual(account["scalper"]["status"]["phase"], "cooling_down")
        self.assertEqual(len(fills), 1)

    def test_gap_preserves_known_partial_inventory_and_does_not_waive_wait(self):
        account, _ = maker_step(self.account, market(102, [trade(1, 101, "100", "2")]), 102, self.config, True)
        q = {**market(104, [trade(2, 103, "100", "100")]), "gap": True}
        after, fills = maker_step(account, q, 104, self.config, False)
        self.assertEqual(after["qqq"], account["qqq"])
        self.assertEqual(after["orders"], [])
        self.assertEqual(fills, [])
        after, _ = maker_step(after, market(106, mark="99"), 106, self.config, True)
        self.assertEqual([o["side"] for o in after["orders"]], ["sell"])
        self.assertEqual(after["scalper"]["status"]["phase"], "cooling_down")

    def test_pausing_entries_keeps_known_fills_and_cancels_unfilled_remainder(self):
        account, fills = maker_step(self.account, market(102, [trade(1, 101, "100", "2")]), 102, self.config, False)
        self.assertEqual(len(fills), 1)
        self.assertEqual(account["orders"][0]["cancel_ts"], 102.3)
        account, fills = maker_step(account, market(104, [trade(2, 103, "100", "100")]), 104, self.config, False)
        self.assertEqual(fills, [])
        self.assertEqual(D(account["qqq"]["qty"]), 2)
        self.assertEqual([o["side"] for o in account["orders"]], ["sell"])

    def test_capacity_includes_pending_partial_batch(self):
        cfg = replace(self.config, settings=replace(self.config.settings, grid_count=1))
        account = self.filled()
        account, _ = maker_step(account, market(1000, mark="90"), 1000, cfg, True)
        self.assertEqual(account["scalper"]["status"]["phase"], "capacity_full")
        self.assertEqual(len(account["slots"]), 1)

    def test_identity_preserves_legacy_and_separates_model_and_timings(self):
        legacy = replace(self.config, scalper=None, take_profit_percent=None)
        old = json.loads(legacy.strategy_identity())
        self.assertEqual(old["model_version"], 1)
        self.assertNotIn("scalper", old)
        self.assertNotIn("take_profit_percent", old)
        self.assertNotEqual(legacy.strategy_identity(), self.config.strategy_identity())
        self.assertNotEqual(self.config.strategy_identity(), replace(self.config, scalper=ScalperSettings(wait_seconds=400)).strategy_identity())
        for value in (0, -1, True, float("nan"), float("inf")):
            with self.assertRaises(GridError):
                ScalperSettings(wait_seconds=value).validate()


class DistanceFreeScalperTests(ScalperTests):
    # Exercise the existing order-lifetime cases against v2 as well as v1.
    def setUp(self):
        self.config = replace(config(), scalper=ScalperSettings(model=CURRENT_MODEL))
        self.account, _ = maker_step(initial_account(), market(100), 100, self.config, True)

    def test_single_near_book_entry_and_independent_tp_after_complete_fill(self):
        account = self.filled()
        self.assertEqual(account["scalper"]["status"]["phase"], "cooling_down")
        self.assertEqual([o["side"] for o in account["orders"]], ["sell"])
        _, fills = maker_step(self.account, market(102, [trade(1, 101, "100", "10")]), 102, self.config, True)
        self.assertEqual(fills[0]["maker_model"], CURRENT_MODEL)

    def test_grid_gate_uses_ask_tp_and_strict_comparison(self):
        # v2 removes eligibility distance, but preserves TP and Maker limit pricing.
        q = market(100, bid="99.98", ask="100")
        for step in ("0.05", "0.1", "0.2"):
            cfg = replace(self.config, grid_step_percent=step, take_profit_percent="2")
            entry, target, allowed = candidate_prices(q, [D("99.97")], cfg)
            self.assertTrue(allowed)
            self.assertEqual((entry, target), (D("99.96"), D("101.95")))
            self.assertFalse(candidate_prices(q, [D("99.97")], replace(cfg, scalper=ScalperSettings()))[2])

    def test_failed_price_gate_consumes_close_count_cooldown_waiver(self):
        account = self.filled()
        account["scalper"]["last_close_count"] = 2
        account, _ = maker_step(account, market(104, mark="100.02"), 104, self.config, True)
        self.assertEqual(account["scalper"]["status"]["phase"], "opening")
        self.assertTrue(account["scalper"]["status"]["cooldown_waived"])

    def test_expired_cooldown_opens_even_when_legacy_distance_fails(self):
        account = self.filled()
        legacy = replace(self.config, scalper=ScalperSettings())
        for now, phase in ((214.5, "cooling_down"), (214.6, "opening")):
            after, _ = maker_step(account, market(now, mark="100.02"), now, self.config, True)
            self.assertEqual(after["scalper"]["status"]["phase"], phase)
        old, _ = maker_step(account, market(214.6, mark="100.02"), 214.6, legacy, True)
        self.assertEqual(old["scalper"]["status"]["phase"], "grid_blocked")
        self.assertFalse(after["scalper"]["status"]["entry_distance_enabled"])
        self.assertIsNone(after["scalper"]["status"]["grid_allowed"])
        self.assertEqual(len([o for o in after["orders"] if o["side"] == "buy"]), 1)
        self.assertEqual(after["slots"][-1]["tp_price"], "100.07")


class DurableScalperTests(unittest.TestCase):
    def test_restart_recovery_and_reset_preserve_or_clear_decisions_exactly(self):
        for model in ("perp_dex_scalper_v1", CURRENT_MODEL):
            with self.subTest(model=model):
                self.check_restart_recovery_and_reset(model)

    def check_restart_recovery_and_reset(self, model):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = ScalperSettings(model=model)
            configs = {name: replace(config(), name=name, state_file=str(root / (name + ".sqlite3")), hedge_threshold_usdc="500", scalper=policy) for name in ("a", "b")}
            experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, configs, QQQSettings(), QQQPricing(mode="exact_quantity"), scalper=policy)
            def frame(cohort, ts, trades=()):
                f = cohort.prepare_frame(ts, {"lighter": market(ts, trades), "var": quote(ts), "allow_entries": True, "reason": ""}, data_kind="synthetic")
                for name, plan in f.plans.items():
                    qty = abs(D(plan["target"]) - D(cohort.stores[name].account()["us100"]["qty"]))
                    if qty:
                        f.quotes[format(qty.normalize(), "f")] = quote(ts, qty)
                return f
            with QQQCohort(experiment) as cohort:
                cohort.ingest(frame(cohort, 100))
                f = frame(cohort, 102, [trade(1, 101, "100", "10")])
                with patch.object(cohort.engines["b"], "apply", side_effect=RuntimeError("power loss")):
                    with self.assertRaises(RuntimeError):
                        cohort.ingest(f)
            with QQQCohort(experiment) as cohort:
                before = cohort.latest()
                self.assertEqual(before["sample_count"], 2)
                self.assertEqual(before["scalper"]["wait_seconds"], 450)
                for store in cohort.stores.values():
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 2)
                    self.assertEqual(store.account()["scalper"]["last_entry_ts"], 102)
                cohort.recover()
                self.assertEqual(cohort.latest(), before)
                next_frame = frame(cohort, 104)
                result = cohort.ingest(next_frame)
                self.assertTrue(all(r["scalper"]["phase"] == "cooling_down" for r in result["scenarios"]))
                request_reset(experiment, read_state(experiment)["generation"])
                self.assertTrue(process_reset(cohort))
                self.assertIsNone(cohort.latest())
                result = cohort.ingest(frame(cohort, 106))
                self.assertTrue(all(r["scalper"]["active_entries"] == 1 for r in result["scenarios"]))
                self.assertTrue(all(store.account()["scalper"]["last_entry_ts"] is None for store in cohort.stores.values()))

    def test_v2_cannot_resume_v1_economic_identity_in_same_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(config(), state_file=str(root / "a.sqlite3"))
            old = QQQExperiment(SimpleNamespace(poll_seconds=2), root, {"a":cfg}, QQQSettings(), scalper=cfg.scalper)
            with QQQCohort(old):
                pass
            policy = ScalperSettings(model=CURRENT_MODEL)
            new = replace(old, scenarios={"a":replace(cfg, scalper=policy)}, scalper=policy)
            with self.assertRaises(GridError):
                with QQQCohort(new):
                    pass
            with QQQCohort(old):
                pass


if __name__ == "__main__":
    unittest.main()
