"""Protect partial entries without reinterpreting journaled observations."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from variational_grid.models import D, GridError
from variational_grid.qqq_comparison import QQQCohort, QQQExperiment
from variational_grid.qqq_hedge import initial_account, maker_step
from variational_grid.qqq_scalper import CURRENT_MODEL, SAFETY_POLICY, ScalperSettings
from test_qqq import quote, trade
from test_qqq_execution import book
from test_qqq_scalper import config


def observation(ts, *args, **kwargs):
    return book(ts, *args, scalper_safety_policy=SAFETY_POLICY, **kwargs)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(config(), scalper=ScalperSettings(model=CURRENT_MODEL))
        self.opening, _ = maker_step(initial_account(), observation(100, "99.99", "100.01"), 100, self.config, True)

    def step(self, account, now, trades=(), allow=True, **kwargs):
        return maker_step(account, observation(now, "99.99", "100.01", trades=trades, **kwargs), now, self.config, allow)

    def test_partial_quantity_is_protected_before_remaining_entry_finishes(self):
        account, fills = self.step(self.opening, 102, [trade(1, 101, "100", "2")])
        self.assertEqual(len(fills), 1)
        self.assertTrue(account["slots"][0]["entry_pending"])
        self.assertEqual([(o["side"], D(o["remaining"])) for o in account["orders"]], [("buy", D(8)), ("sell", D(2))])
        account, _ = self.step(account, 104, [trade(2, 103, "100", "3")])
        self.assertEqual(sum(D(o["remaining"]) for o in account["orders"] if o["side"] == "sell"), D(5))
        self.assertEqual(account["scalper"]["status"]["take_profit_blockers"], [])

    def test_tp_can_close_partial_before_entry_remainder_fills_without_over_sell(self):
        account, _ = self.step(self.opening, 102, [trade(1, 101, "100", "2")])
        account, fills = maker_step(account, observation(104), 104, self.config, True)
        self.assertEqual([f["reason"] for f in fills], ["taker_take_profit"])
        self.assertEqual(D(account["qqq"]["qty"]), 0)
        self.assertTrue(account["slots"][0]["entry_pending"])
        account, _ = self.step(account, 106, [trade(2, 105, "100", "8")])
        self.assertEqual(D(account["qqq"]["qty"]), 8)
        self.assertEqual(sum(D(o["remaining"]) for o in account["orders"] if o["side"] == "sell"), 8)
        self.assertFalse(account["slots"][0]["entry_pending"])

    def test_partial_dust_uses_priced_ioc_without_waiting_for_cancel(self):
        account, _ = self.step(self.opening, 102, [trade(1, 101, "100", ".0008")])
        tp = next(o for o in account["orders"] if o["side"] == "sell")
        self.assertEqual((tp["time_in_force"], tp["price"], D(tp["remaining"])), ("IOC", "100.05", D(".0008")))

    def test_stale_pause_cancels_entry_but_keeps_inflight_fills(self):
        stale = observation(100, trades=[trade(1, 101, "100", "2")])
        account, fills = maker_step(self.opening, stale, 200, self.config, False)
        self.assertEqual(D(fills[0]["qty"]), 2)
        self.assertEqual(account["orders"][0]["cancel_ts"], 200.3)
        self.assertEqual(account["scalper"]["status"]["take_profit_blockers"][0]["quantity"], "2")
        self.assertEqual(account["scalper"]["status"]["phase"], "market_gap")
        account, fills = self.step(account, 202, [trade(2, 200.2, "100", "1"), trade(3, 200.3, "100", "7")], allow=False)
        self.assertEqual(sum(D(f["qty"]) for f in fills), 1)
        self.assertEqual([(o["side"], D(o["remaining"])) for o in account["orders"]], [("sell", D(3))])

    def test_stale_depth_does_not_reanchor_unknown_queue(self):
        self.opening["orders"][0]["queue"] = None
        old_active = self.opening["orders"][0]["active_ts"]
        account, _ = maker_step(self.opening, observation(100), 200, self.config, False)
        self.assertIsNone(account["orders"][0]["queue"])
        self.assertEqual(account["orders"][0]["active_ts"], old_active)

    def test_unversioned_partial_and_stale_frames_keep_old_semantics(self):
        account, _ = maker_step(self.opening, book(102, trades=[trade(1, 101, "100", "2")]), 102, self.config, True)
        self.assertEqual([o["side"] for o in account["orders"]], ["buy"])
        account, _ = maker_step(account, book(102), 200, self.config, False)
        self.assertIsNone(account["orders"][0]["cancel_ts"])
        self.assertNotIn("partial_entry_protection", account["scalper"]["status"])

    def test_half_commit_recovery_preserves_both_policy_generations(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                configs = {n: replace(self.config, name=n, state_file=str(root / (n + ".sqlite3"))) for n in ("a", "b")}
                experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, configs, self.config.settings, scalper=self.config.scalper)
                with QQQCohort(experiment) as cohort:
                    for store in cohort.stores.values():
                        with store.transaction():
                            store.set("account", json.dumps(self.opening))
                    q = observation(102, trades=[trade(1, 101, "100", "2")])
                    frame = cohort.prepare_frame(102, {"lighter": q, "var": None, "allow_entries": False, "reason": ""}, data_kind="synthetic")
                    if legacy:
                        del frame.market["lighter"]["scalper_safety_policy"]
                    with patch.object(cohort.engines["b"], "apply", side_effect=RuntimeError("crash")):
                        with self.assertRaises(RuntimeError):
                            cohort.ingest(frame)
                with QQQCohort(experiment) as cohort:
                    accounts = [s.account() for s in cohort.stores.values()]
                    self.assertEqual(accounts[0], accounts[1])
                    self.assertEqual(accounts[0]["qqq"]["fill_count"], 1)
                    self.assertEqual(any(o["side"] == "sell" for o in accounts[0]["orders"]), not legacy)
                    fresh = cohort.prepare_frame(104, {"lighter": book(104), "var": None, "allow_entries": False, "reason": ""}, data_kind="synthetic")
                    fresh.market["lighter"]["scalper_safety_policy"] = "typo"
                    with self.assertRaisesRegex(GridError, "Unknown QQQ scalper safety"):
                        fresh.validate(experiment)


if __name__ == "__main__":
    unittest.main()
