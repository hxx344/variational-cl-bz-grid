"""Small TP recovery without resetting v3 accounts or rewriting old observations."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from variational_grid.models import D, GridError
from variational_grid.qqq_comparison import QQQCohort, QQQExperiment
from variational_grid.qqq_execution import TAKE_PROFIT_POLICY, submit_take_profit
from variational_grid.qqq_hedge import initial_account, maker_step
from variational_grid.qqq_scalper import CURRENT_MODEL, ScalperSettings
from test_qqq import quote, trade
from test_qqq_execution import book
from test_qqq_scalper import config


class DustExitTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(config(), scalper=ScalperSettings(model=CURRENT_MODEL))

    def partial(self, quantity=".0008"):
        account, _ = maker_step(initial_account(), book(100, "99.99", "100.01"), 100, self.config, True)
        account, _ = maker_step(account, book(102, trades=[trade(1, 101, "100", quantity)]), 102, self.config, True)
        account, _ = maker_step(account, book(120), 120, self.config, False)
        return account

    def finalized(self, quantity=".0008", legacy=False):
        q = book(122)
        if legacy:
            del q["take_profit_policy"]
        return maker_step(self.partial(quantity), q, 122, self.config, True)[0]

    def test_existing_point_zero_zero_zero_eight_block_recovers_and_allows_entries(self):
        before = self.finalized(legacy=True)
        self.assertEqual(before["slots"][0]["tp_rejection"], "below_min_quantity")
        old_leg = dict(before["qqq"])
        after, fills = maker_step(before, book(1000, "99.99", "100.01"), 1000, self.config, True)
        self.assertEqual(fills, [])
        self.assertEqual(after["qqq"], old_leg)
        self.assertEqual(after["scalper"]["status"]["phase"], "opening")
        self.assertIsNone(after["slots"][0]["tp_rejection"])
        tp = next(o for o in after["orders"] if o["side"] == "sell")
        self.assertEqual((tp["time_in_force"], D(tp["remaining"]), tp["price"]), ("IOC", D(".0008"), "100.05"))
        self.assertEqual(after["scalper"]["status"]["small_take_profits"],
                         [{"slot": 1, "quantity": "0.0008", "limit": "100.05"}])
        self.assertEqual(after["scalper"]["status"]["take_profit_blockers"], [])

    def test_minimum_quantity_and_notional_use_ioc_but_step_and_positive_still_apply(self):
        for quantity in (".0008", ".01"):
            with self.subTest(quantity=quantity):
                account = self.finalized(quantity)
                self.assertEqual(account["orders"][0]["time_in_force"], "IOC")
                self.assertEqual(D(account["qqq"]["qty"]), D(quantity))
        account = self.finalized()
        count = len(account["orders"])
        for quantity, reason in ((".00008", "quantity_off_step"), ("0", "invalid_quantity"), ("-.01", "invalid_quantity")):
            self.assertEqual(submit_take_profit(account, account["slots"][0], D(quantity), book(124), 124, self.config.settings), reason)
        self.assertEqual(len(account["orders"]), count)

    def test_unmarketable_ioc_expires_and_retries_without_resting_or_old_trade_fills(self):
        account = self.finalized()
        old_id = account["orders"][0]["id"]
        q = book(124, "99.99", "100.01", trades=[trade(2, 123, "100.10", "1", "buy")])
        after, fills = maker_step(account, q, 124, self.config, False)
        self.assertEqual(fills, [])
        self.assertEqual(after["qqq"], account["qqq"])
        order = after["orders"][0]
        self.assertGreater(order["id"], old_id)
        self.assertTrue(order["activation_pending"])
        self.assertIsNone(order["queue"])
        self.assertNotIn("rested_ts", order)
        self.assertEqual(order["submitted_ts"], 124)
        again, fills = maker_step(after, q, 124, self.config, False)
        self.assertEqual(fills, [])
        self.assertEqual(again["orders"], after["orders"])

    def test_partial_depth_ioc_retry_keeps_limit_and_charges_only_actual_fills(self):
        self.config = replace(self.config, settings=replace(self.config.settings, lighter_fee_bps="10"))
        before = self.finalized()
        q = book(124, bids=[["100.09", ".0003"], ["100.04", "5"]])
        after, fills = maker_step(before, q, 124, self.config, False)
        self.assertEqual([(f["qty"], f["price"], f["time_in_force"]) for f in fills], [("0.0003", "100.09", "IOC")])
        self.assertEqual(D(after["qqq"]["qty"]), D(".0005"))
        self.assertEqual(D(after["qqq"]["fees_usdc"]) - D(before["qqq"]["fees_usdc"]), D(".000030027"))
        self.assertEqual(after["orders"][0]["price"], "100.05")
        for now, source in ((124.1, 124.1), (126, 124), (126, None), (126, 127), (150, 126)):
            q = book(now)
            q["source_ts"] = source
            untouched, rejected = maker_step(after, q, now, self.config, False)
            self.assertEqual(rejected, [])
            self.assertEqual(untouched["orders"], after["orders"])
        final, fills = maker_step(after, book(126), 126, self.config, False)
        self.assertEqual(D(final["qqq"]["qty"]), 0)
        self.assertEqual(sum(D(f["qty"]) for f in fills), D(".0005"))
        self.assertEqual(final["orders"], [])

    def test_ioc_and_gtt_share_arrival_depth(self):
        account = self.finalized()
        other = {**account["slots"][0], "slot": 2, "qty": "1", "entered": "1"}
        account["slots"].append(other)
        account["qqq"]["qty"] = "1.0008"
        submit_take_profit(account, other, D(1), book(122), 122, self.config.settings)
        q = book(124, bids=[["100.09", ".0005"], ["100.04", "10"]])
        after, fills = maker_step(account, q, 124, self.config, False)
        self.assertEqual(sum(D(f["qty"]) for f in fills), D(".0005"))
        self.assertEqual(D(after["qqq"]["qty"]), D("1.0003"))
        self.assertEqual({o["time_in_force"] for o in after["orders"]}, {"IOC", "GTT"})
        self.assertEqual(sum(D(o["remaining"]) for o in after["orders"]), D("1.0003"))

    def test_gap_recovery_of_legal_gtt_dust_preserves_inventory_until_fresh_ioc(self):
        account, _ = maker_step(initial_account(), book(100, "99.99", "100.01"), 100, self.config, True)
        account, _ = maker_step(account, book(102, trades=[trade(1, 101, "100", "10")]), 102, self.config, True)
        account, _ = maker_step(account, book(104, bids=[["100.09", "9.9992"], ["100.04", "10"]]), 104, self.config, False)
        self.assertEqual(account["orders"][0]["time_in_force"], "GTT")
        self.assertEqual(D(account["qqq"]["qty"]), D(".0008"))
        account, fills = maker_step(account, book(106, gap=True), 106, self.config, False)
        self.assertEqual(fills, [])
        self.assertEqual(account["orders"], [])
        account, fills = maker_step(account, book(108), 108, self.config, False)
        self.assertEqual(fills, [])
        self.assertEqual(account["orders"][0]["time_in_force"], "IOC")
        account, fills = maker_step(account, book(110), 110, self.config, False)
        self.assertEqual(D(account["qqq"]["qty"]), 0)
        self.assertEqual(D(fills[0]["qty"]), D(".0008"))

    def test_zero_latency_still_requires_a_new_source_observation_for_ioc(self):
        self.config = replace(self.config, settings=replace(self.config.settings, maker_latency_ms=0))
        account = self.finalized()
        same, fills = maker_step(account, book(122), 122, self.config, False)
        self.assertEqual(fills, [])
        self.assertEqual(same["orders"], account["orders"])
        after, fills = maker_step(same, book(124), 124, self.config, False)
        self.assertEqual(D(after["qqq"]["qty"]), 0)
        self.assertEqual(len(fills), 1)

    def experiment(self, root):
        configs = {name: replace(self.config, name=name, state_file=str(root / (name + ".sqlite3"))) for name in ("a", "b")}
        return QQQExperiment(SimpleNamespace(poll_seconds=2), root, configs, self.config.settings, scalper=self.config.scalper)

    def test_v3_reopen_and_ioc_half_commit_recovery_preserve_ledger_and_fill_once(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = self.experiment(Path(directory))
            before = self.finalized(legacy=True)
            with QQQCohort(experiment) as cohort:
                for store in cohort.stores.values():
                    with store.transaction():
                        store.set("account", json.dumps(before))
            with QQQCohort(experiment) as cohort:
                self.assertTrue(all(s.account() == before for s in cohort.stores.values()))
                raw = {"lighter": book(124), "var": quote(124), "allow_entries": False, "reason": ""}
                del raw["lighter"]["take_profit_policy"]
                frame = cohort.prepare_frame(124, raw, data_kind="synthetic")
                self.assertNotIn("take_profit_policy", raw["lighter"])
                self.assertEqual(frame.market["lighter"]["take_profit_policy"], TAKE_PROFIT_POLICY)
                cohort.ingest(frame)
                frame = cohort.prepare_frame(126, {**raw, "lighter": book(126), "var": quote(126)}, data_kind="synthetic")
                with patch.object(cohort.engines["b"], "apply", side_effect=RuntimeError("power loss")):
                    with self.assertRaises(RuntimeError):
                        cohort.ingest(frame)
            with QQQCohort(experiment) as cohort:
                self.assertEqual(cohort.latest()["ts"], 126)
                for store in cohort.stores.values():
                    account = store.account()
                    self.assertEqual(D(account["qqq"]["qty"]), 0)
                    self.assertEqual(account["qqq"]["fill_count"], before["qqq"]["fill_count"] + 1)
                    fills = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM fills")]
                    self.assertEqual(sum(f.get("time_in_force") == "IOC" for f in fills), 1)
                accounts = [s.account() for s in cohort.stores.values()]
                self.assertEqual(accounts[0], accounts[1])

    def test_old_half_committed_journal_keeps_old_policy_until_next_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = self.experiment(Path(directory))
            before = self.finalized(legacy=True)
            with QQQCohort(experiment) as cohort:
                for store in cohort.stores.values():
                    with store.transaction():
                        store.set("account", json.dumps(before))
                frame = cohort.prepare_frame(1000, {"lighter": book(1000), "var": quote(1000), "allow_entries": True, "reason": ""}, data_kind="synthetic")
                del frame.market["lighter"]["take_profit_policy"]
                with patch.object(cohort.engines["b"], "apply", side_effect=RuntimeError("old process stopped")):
                    with self.assertRaises(RuntimeError):
                        cohort.ingest(frame)
            with QQQCohort(experiment) as cohort:
                accounts = [s.account() for s in cohort.stores.values()]
                self.assertEqual(accounts[0], accounts[1])
                self.assertEqual(accounts[0]["orders"], [])
                self.assertEqual(accounts[0]["scalper"]["status"]["take_profit_execution"], "gtt_limit_v1")
                frame = cohort.prepare_frame(1002, {"lighter": book(1002), "var": quote(1002), "allow_entries": True, "reason": ""}, data_kind="synthetic")
                frame.market["lighter"]["take_profit_policy"] = "misspelled"
                with self.assertRaisesRegex(GridError, "Unknown QQQ take-profit"):
                    frame.validate(experiment)
                frame.market["lighter"]["take_profit_policy"] = TAKE_PROFIT_POLICY
                cohort.ingest(frame)
                self.assertTrue(all(s.account()["scalper"]["status"]["phase"] == "opening" for s in cohort.stores.values()))
                self.assertTrue(all(any(o.get("time_in_force") == "IOC" for o in s.account()["orders"]) for s in cohort.stores.values()))


if __name__ == "__main__":
    unittest.main()
