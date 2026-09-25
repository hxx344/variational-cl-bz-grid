"""Regressions for marketable TP arrival, replay, depth and legacy semantics."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from variational_grid.models import D, GridError
from variational_grid.qqq_comparison import QQQCohort, QQQExperiment
from variational_grid.qqq_execution import submit_take_profit
from variational_grid.qqq_hedge import initial_account, maker_step
from variational_grid.qqq_scalper import CURRENT_MODEL, ScalperSettings
from test_qqq import market, quote, trade
from test_qqq_scalper import config


def book(ts, bid="100.09", ask="100.11", bids=None, asks=None, trades=(), **extra):
    result = market(ts, trades, mark=str((D(bid) + D(ask)) / 2), bid=bid, ask=ask)
    result.update(bids=bids if bids is not None else [[bid, "100"]],
                  asks=asks if asks is not None else [[ask, "100"]], source_ts=ts, **extra)
    return result


class GTTTakeProfitTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(config(), scalper=ScalperSettings(model=CURRENT_MODEL))
        self.opening, _ = maker_step(initial_account(), book(100, "99.99", "100.01"), 100, self.config, True)

    def filled(self, quantity="10", **extra):
        q = book(102, trades=[trade(1, 101, "100", quantity)], **extra)
        return maker_step(self.opening, q, 102, self.config, True)[0]

    def test_crossed_tp_is_submitted_then_executes_visible_bids_at_arrival(self):
        account = self.filled()
        self.assertEqual(D(account["qqq"]["qty"]), 10)
        self.assertTrue(account["orders"][0]["activation_pending"])
        self.assertEqual(account["orders"][0]["price"], "100.05")
        q = book(104, bids=[["100.09", "3"], ["100.07", "4"], ["100.05", "3"], ["100.04", "100"]])
        after, fills = maker_step(account, q, 104, self.config, False)
        self.assertEqual([(f["qty"], f["price"]) for f in fills], [("3", "100.09"), ("4", "100.07"), ("3", "100.05")])
        self.assertEqual(D(after["qqq"]["qty"]), 0)
        self.assertEqual(after["orders"], [])
        self.assertEqual(D(after["qqq"]["realized_gross"]), D(".70"))
        self.assertTrue(all(f["liquidity"] == "taker" and f["quote_source_ts"] == 104 for f in fills))
        replay, repeated = maker_step(after, q, 104, self.config, False)
        self.assertEqual(repeated, [])
        self.assertEqual(replay["qqq"], after["qqq"])

    def test_latency_and_cache_source_time_gate_execution(self):
        account = self.filled()
        for now, source in ((102.1, 102.1), (104, 102), (104, None), (104, 105), (150, 104)):
            with self.subTest(now=now, source=source):
                q = book(now)
                q["source_ts"] = source
                after, fills = maker_step(account, q, now, self.config, False)
                self.assertEqual(fills, [])
                self.assertEqual(D(after["qqq"]["qty"]), 10)
                self.assertTrue(after["orders"][0]["activation_pending"])
        q = book(104)
        after, fills = maker_step(account, q, 104, self.config, False)
        self.assertEqual(D(after["qqq"]["qty"]), 0)
        self.assertTrue(fills)

    def test_zero_latency_never_uses_submission_frame_or_earlier_trades(self):
        self.config = replace(self.config, settings=replace(self.config.settings, maker_latency_ms=0))
        q = book(102, trades=[trade(1, 101, "100", "10"), trade(2, 101.5, "101", "100", "buy")])
        account, fills = maker_step(self.opening, q, 102, self.config, False)
        self.assertEqual(len(fills), 1)
        self.assertEqual(D(account["qqq"]["qty"]), 10)
        q = book(104, "99.99", "100.01", trades=[trade(3, 103, "101", "100", "buy")])
        account, fills = maker_step(account, q, 104, self.config, False)
        self.assertEqual(fills, [])
        self.assertFalse(account["orders"][0]["activation_pending"])
        self.assertEqual(D(account["qqq"]["qty"]), 10)

    def test_partial_depth_remainder_rests_and_does_not_sweep_repeated_books(self):
        q = book(104, bids=[["100.09", "3"], ["100.04", "100"]])
        account, fills = maker_step(self.filled(), q, 104, self.config, False)
        self.assertEqual(sum(D(f["qty"]) for f in fills), 3)
        self.assertEqual(D(account["orders"][0]["remaining"]), 7)
        for now in (104, 106):
            account, fills = maker_step(account, book(now), now, self.config, False)
            self.assertEqual(fills, [])
            self.assertEqual(D(account["qqq"]["qty"]), 7)
        account, fills = maker_step(account, book(108, trades=[trade(4, 107, "100.05", "7", "buy")]), 108, self.config, False)
        self.assertEqual(D(account["qqq"]["qty"]), 0)
        self.assertEqual(fills[0]["reason"], "maker_take_profit")
        self.assertEqual(fills[0]["liquidity"], "maker")

    def test_all_pending_batches_share_depth_and_cannot_oversell(self):
        account = self.filled()
        first = account["slots"][0]
        first["qty"] = first["entered"] = "5"
        second = {**first, "slot": 2}
        account["slots"].append(second)
        account["orders"][0]["remaining"] = "5"
        submit_take_profit(account, second, D(5), book(102), 102, self.config.settings)
        q = book(104, bids=[["100.09", "7"], ["100.04", "100"]])
        account, fills = maker_step(account, q, 104, self.config, False)
        self.assertEqual(sum(D(f["qty"]) for f in fills), 7)
        self.assertEqual(D(account["qqq"]["qty"]), 3)
        self.assertEqual([(s["slot"], D(s["qty"])) for s in account["slots"]], [(2, D(3))])
        self.assertEqual(D(account["orders"][0]["remaining"]), 3)

    def test_valid_order_can_leave_residual_below_new_order_minimum(self):
        q = book(104, bids=[["100.09", "9.999"], ["100.04", "100"]])
        account, _ = maker_step(self.filled(), q, 104, self.config, False)
        self.assertEqual(D(account["orders"][0]["remaining"]), D(".001"))
        account, fills = maker_step(account, book(106, trades=[trade(2, 105, "100.05", ".001", "buy")]), 106, self.config, False)
        self.assertEqual(D(account["qqq"]["qty"]), 0)
        self.assertEqual(len(fills), 1)

    def test_finalized_small_partial_preserves_inventory_and_reports_minimum(self):
        for quantity, reason in ((".001", "below_min_quantity"), (".01", "below_min_notional")):
            with self.subTest(quantity=quantity):
                account = self.filled(quantity)
                account, _ = maker_step(account, book(120), 120, self.config, False)
                account, fills = maker_step(account, book(122), 122, self.config, True)
                self.assertEqual(fills, [])
                self.assertEqual(D(account["qqq"]["qty"]), D(quantity))
                status = account["scalper"]["status"]
                self.assertEqual(status["phase"], "take_profit_pending")
                self.assertEqual(status["take_profit_blockers"][0]["reason"], reason)
                self.assertEqual(account["orders"], [])

    def test_gap_recovery_requeues_without_filling_missing_interval(self):
        account = self.filled()
        account, fills = maker_step(account, book(104, gap=True), 104, self.config, False)
        self.assertEqual(fills, [])
        self.assertEqual(account["orders"], [])
        account, fills = maker_step(account, book(106), 106, self.config, False)
        self.assertEqual(fills, [])
        self.assertTrue(account["orders"][0]["activation_pending"])
        account, fills = maker_step(account, book(108), 108, self.config, False)
        self.assertEqual(D(account["qqq"]["qty"]), 0)
        self.assertEqual(len(fills), 1)

    def test_invalid_or_stale_market_never_executes_new_tp(self):
        for q in (book(104, ready=False), book(104, gap=True), book(80),
                  book(104, bids=[["99.99", "100"]])):
            with self.subTest(q=q):
                account, fills = maker_step(self.filled(), q, 104, self.config, False)
                self.assertEqual(fills, [])
                self.assertEqual(D(account["qqq"]["qty"]), 10)

    def test_original_models_keep_their_post_only_exit_semantics(self):
        for model in ("perp_dex_scalper_v1", "perp_dex_scalper_v2"):
            cfg = replace(self.config, scalper=ScalperSettings(model=model))
            account, _ = maker_step(self.opening, book(102, trades=[trade(1, 101, "100", "10")]), 102, cfg, True)
            self.assertEqual(account["orders"], [])
            self.assertEqual(D(account["qqq"]["qty"]), 10)

    def test_v3_preserves_cooldown_distance_free_entries_and_capacity(self):
        account, _ = maker_step(self.filled(), book(104, "100.01", "100.03"), 104, self.config, True)
        self.assertEqual(account["scalper"]["status"]["phase"], "cooling_down")
        limited = replace(self.config, settings=replace(self.config.settings, grid_count=1))
        full, _ = maker_step(account, book(216, "100.01", "100.03"), 216, limited, True)
        self.assertEqual(full["scalper"]["status"]["phase"], "capacity_full")
        after, _ = maker_step(account, book(216, "100.01", "100.03"), 216, self.config, True)
        self.assertEqual(after["scalper"]["status"]["phase"], "opening")
        self.assertFalse(after["scalper"]["status"]["entry_distance_enabled"])
        self.assertEqual(sum(o["side"] == "buy" for o in after["orders"]), 1)

    def test_taker_exit_applies_configured_fees_and_can_release_next_entry(self):
        self.config = replace(self.config, settings=replace(self.config.settings, lighter_fee_bps="10"))
        account, fills = maker_step(self.filled(), book(104), 104, self.config, True)
        self.assertEqual(D(account["qqq"]["fees_usdc"]), D("2.0009"))
        self.assertEqual(D(fills[0]["fee"]), D("1.0009"))
        self.assertEqual(account["scalper"]["status"]["phase"], "opening")
        self.assertTrue(account["scalper"]["status"]["cooldown_waived"])

    def test_resting_queue_uses_arrival_time_and_conservative_visible_queue(self):
        q = book(104, "99.99", "100.05", asks=[["100.05", "2"], ["100.06", "5"]])
        account, _ = maker_step(self.filled(), q, 104, self.config, False)
        account, fills = maker_step(account, book(106, trades=[trade(2, 104, "100.05", "100", "buy"), trade(3, 105, "100.05", "3", "buy")]), 106, self.config, False)
        self.assertEqual(sum(D(f["qty"]) for f in fills), 1)
        self.assertEqual(D(account["qqq"]["qty"]), 9)

    def test_v3_identity_does_not_resume_v2_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config = replace(self.config, state_file=str(root / "a.sqlite3"), scalper=ScalperSettings(model="perp_dex_scalper_v2"))
            old = QQQExperiment(SimpleNamespace(poll_seconds=2), root, {"a": old_config}, old_config.settings, scalper=old_config.scalper)
            with QQQCohort(old):
                pass
            new = replace(old, scenarios={"a": replace(old_config, scalper=self.config.scalper)}, scalper=self.config.scalper)
            with self.assertRaises(GridError):
                with QQQCohort(new):
                    pass

    def test_journal_recovery_does_not_duplicate_taker_fills(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = {name: replace(self.config, name=name, state_file=str(root / (name + ".sqlite3"))) for name in ("a", "b")}
            experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, configs, self.config.settings, scalper=self.config.scalper)
            with QQQCohort(experiment) as cohort:
                for now, sample in ((100, book(100, "99.99", "100.01")), (102, book(102, trades=[trade(1, 101, "100", "10")]))):
                    frame = cohort.prepare_frame(now, {"lighter": sample, "var": quote(now), "allow_entries": True, "reason": ""}, data_kind="synthetic")
                    cohort.ingest(frame)
                frame = cohort.prepare_frame(104, {"lighter": book(104), "var": quote(104), "allow_entries": False, "reason": ""}, data_kind="synthetic")
                with patch.object(cohort.engines["b"], "apply", side_effect=RuntimeError("power loss")):
                    with self.assertRaises(RuntimeError):
                        cohort.ingest(frame)
            with QQQCohort(experiment) as cohort:
                self.assertEqual(cohort.latest()["ts"], 104)
                for store in cohort.stores.values():
                    self.assertEqual(D(store.account()["qqq"]["qty"]), 0)
                    fills = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM fills")]
                    self.assertEqual(sum(f["reason"] == "taker_take_profit" for f in fills), 1)
                with self.assertRaisesRegex(GridError, "plan does not match"):
                    cohort.ingest(frame)
                self.assertTrue(all(store.account()["qqq"]["fill_count"] == 2 for store in cohort.stores.values()))


if __name__ == "__main__":
    unittest.main()
