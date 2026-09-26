"""Cross-venue closure, durable cancellation, and fresh-only order recovery."""
from dataclasses import replace
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from variational_grid.models import D, GridError
from variational_grid.qqq_comparison import QQQCohort, QQQExperiment, QQQMarketFeed
from variational_grid.qqq_hedge import initial_account, maker_step
from variational_grid.qqq_pause import PAIR_PAUSE_POLICY, pair_step
from variational_grid.qqq_pricing import QQQPricing
from variational_grid.qqq_scalper import CURRENT_MODEL, SAFETY_POLICY, ScalperSettings
from test_qqq import quote, trade
from test_qqq_execution import book
from test_qqq_scalper import config
from test_qqq_market import LighterClient, NOW, Opener, Response, metadata


def observation(ts, *, closed=None, var=None, q=None, allow=True):
    q = q or book(ts, "99.99", "100.01", scalper_safety_policy=SAFETY_POLICY,
                  market_open=closed != "lighter", metadata_ts=ts)
    v = var or quote(ts)
    if closed == "var":
        v = None
    return {"lighter": q, "var": v, "allow_entries": allow and closed is None,
            "var_market_state": "closed" if closed == "var" else "open", "reason": "",
            "pair_pause_policy": PAIR_PAUSE_POLICY}


class PairPauseTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(config(), scalper=ScalperSettings(model=CURRENT_MODEL), hedge_threshold_usdc="1")
        self.opening, _ = pair_step(initial_account(), observation(100), 100, self.config)
        q = book(102, "99.99", "100.01", trades=[trade(1, 101, "100", "2")],
                 scalper_safety_policy=SAFETY_POLICY)
        self.partial, _ = pair_step(self.opening, observation(102, q=q), 102, self.config)

    def test_either_closure_cancels_all_pending_order_types_before_any_fill(self):
        for venue in ("lighter", "var"):
            for pending in (True, False):
                with self.subTest(venue=venue, pending=pending):
                    before = copy.deepcopy(self.partial)
                    tp = next(o for o in before["orders"] if o["side"] == "sell")
                    tp["activation_pending"], tp["queue"] = pending, "0"
                    before["orders"][0]["cancel_ts"] = 105
                    q = book(104, trades=[trade(2, 103, "100", "8"), trade(3, 103.1, "101", "2", "buy")],
                             market_open=venue != "lighter", metadata_ts=104)
                    after, fills = pair_step(before, observation(104, closed=venue, q=q), 104, self.config)
                    self.assertEqual(fills, [])
                    self.assertEqual(after["orders"], [])
                    for key in ("qqq", "us100", "next_fill", "next_order"):
                        self.assertEqual(after[key], before[key])
                    self.assertEqual(after["slots"][0]["qty"], "2")
                    self.assertFalse(after["slots"][0]["entry_pending"])
                    self.assertEqual(after["scalper"]["status"]["phase"], "pair_paused")
                    self.assertEqual(after["scalper"]["status"]["take_profit_blockers"], [])

    def test_pause_latch_rejects_unknown_stale_gap_close_only_and_preclosure_cache(self):
        paused, _ = pair_step(self.partial, observation(104, closed="var"), 104, self.config)
        variants = []
        for key, value in (("gap", True), ("close_only", True), ("source_ts", 90), ("metadata_ts", 70)):
            row = observation(106)
            row["lighter"][key] = value
            variants.append(row)
        for key, value in (("ts", 103), ("close_only", True), ("metadata_ts", -100), ("closes_at", 105)):
            row = observation(106)
            row["var"][key] = value
            variants.append(row)
        variants += [{**observation(106), "var": None, "allow_entries": False, "var_market_state": "unknown"},
                     {**observation(106), "allow_entries": False}]
        for market in variants:
            with self.subTest(market=market):
                after, fills = pair_step(paused, market, 106, self.config)
                self.assertTrue(after["pair_pause"]["active"])
                self.assertEqual(after["orders"], [])
                self.assertEqual(fills, [])
                self.assertEqual(after["qqq"], paused["qqq"])

    def test_resume_requeues_existing_tp_at_new_time_without_replaying_trades(self):
        paused, _ = pair_step(self.partial, observation(104, closed="var"), 104, self.config)
        q = book(106, trades=[trade(2, 103, "100", "8"), trade(3, 105, "101", "2", "buy")])
        resumed, fills = pair_step(paused, observation(106, q=q), 106, self.config)
        self.assertFalse(resumed["pair_pause"]["active"])
        self.assertEqual(fills, [])
        tp = next(o for o in resumed["orders"] if o["side"] == "sell")
        self.assertGreaterEqual(tp["id"], paused["next_order"])
        self.assertGreater(tp["active_ts"], 106)
        self.assertEqual((tp["remaining"], tp["price"]), ("2", self.partial["slots"][0]["tp_price"]))
        again, fills = pair_step(resumed, observation(108, q=book(108)), 108, self.config)
        self.assertEqual(D(again["qqq"]["qty"]), 0)
        self.assertEqual([f["reason"] for f in fills], ["taker_take_profit"])

    def test_dust_ioc_is_cancelled_then_recreated_at_original_limit(self):
        q = book(102, "99.99", "100.01", trades=[trade(1, 101, "100", ".0008")],
                 scalper_safety_policy=SAFETY_POLICY)
        dust, _ = pair_step(self.opening, observation(102, q=q), 102, self.config)
        self.assertEqual(dust["orders"][-1]["time_in_force"], "IOC")
        paused, _ = pair_step(dust, observation(104, closed="var"), 104, self.config)
        resumed, fills = pair_step(paused, observation(106), 106, self.config)
        self.assertEqual(fills, [])
        tp = next(o for o in resumed["orders"] if o["side"] == "sell")
        self.assertEqual((tp["time_in_force"], D(tp["remaining"]), tp["price"]), ("IOC", D(".0008"), "100.05"))

    def test_unversioned_frame_retains_original_tp_semantics(self):
        market = observation(104, closed="var", q=book(104))
        del market["pair_pause_policy"]
        after, fills = pair_step(self.partial, market, 104, self.config)
        self.assertEqual(D(after["qqq"]["qty"]), 0)
        self.assertEqual([f["reason"] for f in fills], ["taker_take_profit"])

    def test_unknown_policy_is_rejected_without_changing_account(self):
        market = observation(104)
        market["pair_pause_policy"] = "typo"
        with self.assertRaisesRegex(GridError, "pair pause policy"):
            pair_step(self.partial, market, 104, self.config)

    def test_mid_commit_pause_recovers_identically_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = {n: replace(self.config, name=n, state_file=str(root / (n + ".sqlite3"))) for n in ("a", "b")}
            experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, configs, self.config.settings,
                                       pricing=QQQPricing(mode="exact_quantity"), scalper=self.config.scalper)
            with QQQCohort(experiment) as cohort:
                for store in cohort.stores.values():
                    with store.transaction():
                        store.set("account", json.dumps(self.partial))
                frame = cohort.prepare_frame(104, observation(104, closed="var"), data_kind="synthetic")
                with patch.object(cohort.engines["b"], "apply", side_effect=RuntimeError("crash")):
                    with self.assertRaises(RuntimeError):
                        cohort.ingest(frame)
            with QQQCohort(experiment) as cohort:
                accounts = [s.account() for s in cohort.stores.values()]
                self.assertEqual(accounts[0], accounts[1])
                self.assertTrue(accounts[0]["pair_pause"]["active"])
                self.assertEqual(cohort.latest()["market"]["source_status"], "pair_paused")
                frame = cohort.prepare_frame(106, {**observation(106), "var": None, "allow_entries": False,
                                                  "var_market_state": "unknown"}, data_kind="synthetic")
                rows = cohort.ingest(frame)["scenarios"]
                self.assertTrue(all(r["pair_pause"]["active"] and not r["resting_orders"] for r in rows))
                self.assertTrue(all(r["hedge_status"] == "pair_paused" for r in rows))
                self.assertTrue(all(s.db.execute("SELECT count(*) FROM fills").fetchone()[0] == 0 for s in cohort.stores.values()))

    def test_closed_metadata_then_empty_lighter_book_still_journals_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(self.config, state_file=str(root / "a.sqlite3"))
            experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, {cfg.name: cfg}, cfg.settings,
                                       pricing=QQQPricing(mode="exact_quantity"), scalper=cfg.scalper)
            details = metadata()
            details["order_book_details"][0]["is_frozen"] = True
            lighter = LighterClient(opener=Opener([Response(details, NOW + 2), Response({"code": 200, "bids": [], "asks": []}, NOW + 2)]), clock=lambda: NOW + 2)
            var = SimpleNamespace(market=lambda: quote(NOW + 2))
            with QQQCohort(experiment) as cohort:
                cohort.ingest(cohort.prepare_frame(NOW, observation(NOW), data_kind="synthetic"))
                feed = QQQMarketFeed(experiment, lighter, var)
                with patch("variational_grid.qqq_comparison.time.time", return_value=NOW + 2):
                    frame = feed.next(cohort)
                frame.data_kind = "synthetic"
                self.assertEqual(frame.market["lighter"]["source_ts"], NOW)
                self.assertFalse(frame.market["lighter"]["ready"])
                result = cohort.ingest(frame)["scenarios"][0]
                self.assertTrue(result["pair_pause"]["active"])
                self.assertEqual(result["pair_pause"]["closed_venues"], ["lighter"])
                self.assertEqual(result["resting_orders"], 0)

    def test_lighter_read_failure_does_not_hide_var_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(self.config, state_file=str(root / "a.sqlite3"))
            experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, {cfg.name: cfg}, cfg.settings,
                                       pricing=QQQPricing(mode="exact_quantity"), scalper=cfg.scalper)
            lighter = SimpleNamespace(snapshot=lambda: (_ for _ in ()).throw(GridError("offline")))
            var = SimpleNamespace(market=lambda: {"market_open": False})
            with QQQCohort(experiment) as cohort:
                cohort.ingest(cohort.prepare_frame(NOW, observation(NOW), data_kind="synthetic"))
                with patch("variational_grid.qqq_comparison.time.time", return_value=NOW + 2):
                    frame = QQQMarketFeed(experiment, lighter, var).next(cohort)
                frame.data_kind = "synthetic"
                result = cohort.ingest(frame)["scenarios"][0]
                self.assertEqual(result["pair_pause"]["closed_venues"], ["var"])
                self.assertEqual(result["resting_orders"], 0)

    def test_quote_request_crossing_preclose_boundary_cancels_instead_of_filling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(self.config, state_file=str(root / "a.sqlite3"))
            experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, {cfg.name: cfg}, cfg.settings,
                                       pricing=QQQPricing(mode="exact_quantity"), scalper=cfg.scalper)
            now = [104]
            def delayed_quote(qty):
                now[0] = 106
                return quote(106, qty)
            var = SimpleNamespace(market=lambda: {**quote(104), "closes_at": 405}, quote=delayed_quote)
            lighter = SimpleNamespace(snapshot=lambda: book(104, "99.99", "100.01", trades=[trade(2, 103, "100", "8")]))
            with QQQCohort(experiment) as cohort:
                with cohort.stores[cfg.name].transaction():
                    cohort.stores[cfg.name].set("account", json.dumps(self.partial))
                with patch("variational_grid.qqq_comparison.time.time", side_effect=lambda: now[0]):
                    frame = QQQMarketFeed(experiment, lighter, var).next(cohort)
                self.assertEqual(frame.ts, 106)
                self.assertEqual(frame.market["var_market_state"], "open")
                self.assertEqual(frame.quotes, {})
                result = cohort.ingest(frame)["scenarios"][0]
                self.assertEqual(result["qqq"]["qty"], "2")
                self.assertEqual(result["us100"]["qty"], "0")
                self.assertEqual(result["resting_orders"], 0)
                self.assertEqual(result["pair_pause"]["resume_after_ts"], 405)

    def test_preclose_exact_boundary_and_same_session_cannot_resume(self):
        before, _ = pair_step(self.partial, observation(199.999, var={**quote(199.999), "closes_at": 500}), 199.999, self.config)
        self.assertNotIn("pair_pause", before)
        paused, fills = pair_step(before, observation(200, var={**quote(200), "closes_at": 500}), 200, self.config)
        self.assertEqual(fills, [])
        self.assertEqual(paused["orders"], [])
        self.assertEqual(paused["pair_pause"]["resume_after_ts"], 500)
        self.assertEqual(paused["pair_pause"]["closing_venues"], ["var"])
        self.assertEqual(paused["pair_pause"]["closed_venues"], [])
        for now in (202, 250, 499):
            paused, fills = pair_step(paused, observation(now, var={**quote(now), "closes_at": 500}), now, self.config)
            self.assertTrue(paused["pair_pause"]["active"])
            self.assertEqual(fills, [])
            self.assertEqual(paused["pair_pause"]["resume_after_ts"], 500)
        stale, _ = pair_step(paused, observation(501, var={**quote(499), "closes_at": 2000}), 501, self.config)
        self.assertTrue(stale["pair_pause"]["active"])
        resumed, fills = pair_step(stale, observation(502, var={**quote(502), "closes_at": 2000}), 502, self.config)
        self.assertFalse(resumed["pair_pause"]["active"])
        self.assertEqual(fills, [])
        self.assertEqual(D(resumed["qqq"]["qty"]), 2)

    def test_short_new_session_and_missing_quote_keep_preclose_pause(self):
        paused, _ = pair_step(self.partial, observation(200, var={**quote(200), "closes_at": 500}), 200, self.config)
        short, _ = pair_step(paused, observation(501, var={**quote(501), "closes_at": 801}), 501, self.config)
        self.assertTrue(short["pair_pause"]["active"])
        self.assertEqual(short["pair_pause"]["resume_after_ts"], 801)
        missing = {**observation(202), "var": None, "allow_entries": False, "var_market_state": "unknown",
                   "var_market_closes_at": 500}
        before_close, _ = pair_step(self.partial, missing, 202, self.config)
        self.assertTrue(before_close["pair_pause"]["active"])
        self.assertEqual(before_close["pair_pause"]["resume_after_ts"], 500)
        after_close, _ = pair_step(self.partial, missing, 503, self.config)
        self.assertEqual(after_close["pair_pause"]["closed_venues"], ["var"])

    def test_preclose_lock_survives_restart_with_fresh_same_session_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(self.config, state_file=str(root / "a.sqlite3"))
            experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, {cfg.name: cfg}, cfg.settings,
                                       pricing=QQQPricing(mode="exact_quantity"), scalper=cfg.scalper)
            with QQQCohort(experiment) as cohort:
                cohort.ingest(cohort.prepare_frame(100, observation(100), data_kind="synthetic"))
                cohort.ingest(cohort.prepare_frame(200, observation(200, var={**quote(200), "closes_at": 500}), data_kind="synthetic"))
            with QQQCohort(experiment) as cohort:
                frame = cohort.prepare_frame(202, observation(202, var={**quote(202), "closes_at": 500}), data_kind="synthetic")
                row = cohort.ingest(frame)["scenarios"][0]
                self.assertTrue(row["pair_pause"]["active"])
                self.assertEqual(row["resting_orders"], 0)
                self.assertEqual(row["pair_pause"]["resume_after_ts"], 500)

    def test_unversioned_closure_journal_finishes_original_fills_after_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = {n: replace(self.config, name=n, state_file=str(root / (n + ".sqlite3"))) for n in ("a", "b")}
            experiment = QQQExperiment(SimpleNamespace(poll_seconds=2), root, configs, self.config.settings,
                                       pricing=QQQPricing(mode="exact_quantity"), scalper=self.config.scalper)
            with QQQCohort(experiment) as cohort:
                for store in cohort.stores.values():
                    with store.transaction():
                        store.set("account", json.dumps(self.partial))
                frame = cohort.prepare_frame(104, observation(104, closed="var", q=book(104)), data_kind="synthetic")
                del frame.market["pair_pause_policy"]
                with patch.object(cohort.engines["b"], "apply", side_effect=RuntimeError("crash")):
                    with self.assertRaises(RuntimeError):
                        cohort.ingest(frame)
            with QQQCohort(experiment) as cohort:
                accounts = [s.account() for s in cohort.stores.values()]
                self.assertEqual(accounts[0], accounts[1])
                self.assertEqual(D(accounts[0]["qqq"]["qty"]), 0)
                self.assertNotIn("pair_pause", accounts[0])
                self.assertTrue(all(s.db.execute("SELECT count(*) FROM fills").fetchone()[0] == 1 for s in cohort.stores.values()))


if __name__ == "__main__":
    unittest.main()
