"""Offline execution boundary tests; no sessions, credentials or network."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from variational_grid.execution import (
    ExecutionError, ExecutionJournal, FakeBroker, Fill, MarketState,
    OrderObservation, OrderSpec, RiskLimits, SimulationExecutor, number,
)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "execution.sqlite3"
        self.broker = FakeBroker()
        self.now = 10000.0
        self.limits = RiskLimits("1000", "2000", "4000", "1500", "10", "0.1", 5, 60)
        self.market = {
            "lighter:QQQ": MarketState("100", self.now, self.now, self.now + 600, "1000", "0.01", "0.01"),
            "variational:US100S": MarketState("200", self.now, self.now, self.now + 600, "1000", "0.01", "0.01"),
        }
        self.journal = None
        self.reopen()
        self.assertTrue(self.executor.reconcile())

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def reopen(self):
        if self.journal:
            self.journal.close()
        self.journal = ExecutionJournal(self.path)
        self.executor = SimulationExecutor(self.journal, self.broker, self.limits)

    def submit(self, key="entry", qty="2", *, side="buy", reduce_only=False, venue="lighter"):
        spec = OrderSpec(venue, "QQQ" if venue == "lighter" else "US100S", side, qty, "100" if venue == "lighter" else "200", reduce_only)
        return self.executor.submit(key, spec, self.market, self.now)

    def filled_position(self, qty="2"):
        order = self.submit(qty=qty)
        self.executor.observe(self.broker.fill(order["client_id"], Fill("entry-fill", qty, "100", self.now)))
        return order

    def test_intent_is_committed_before_first_broker_call(self):
        original = self.broker.submit

        def inspect(client_id, spec):
            with closing(sqlite3.connect(self.path)) as db:
                row = db.execute("SELECT client_id,status FROM execution_intents").fetchone()
            self.assertEqual(row, (client_id, "sending"))
            return original(client_id, spec)

        with patch.object(self.broker, "submit", side_effect=inspect):
            self.assertEqual(self.submit()["status"], "ack")

    def test_same_intent_does_not_send_again_and_changed_payload_is_rejected(self):
        first = self.submit(qty="2.0")
        self.assertEqual(self.submit(qty="2")["client_id"], first["client_id"])
        with self.assertRaises(ExecutionError):
            self.submit(qty="3")
        self.assertEqual(self.broker.submit_count, 1)

    def test_lost_ack_restart_reconciles_without_duplicate_order(self):
        self.broker.drop_next_ack = True
        order = self.submit()
        self.assertEqual(order["status"], "unknown")
        self.broker.fill(order["client_id"], Fill("while-offline", "1", "100", self.now))
        self.reopen()
        self.assertEqual(self.submit()["client_id"], order["client_id"])
        with self.assertRaises(ExecutionError):
            self.submit("new")
        self.assertTrue(self.executor.reconcile())
        self.assertEqual(self.journal.order(order["client_id"])["status"], "partial")
        self.assertEqual(self.journal.positions()["lighter:QQQ"], 1)
        self.assertEqual(self.broker.submit_count, 1)
        with self.assertRaises(ExecutionError):
            self.submit("new")  # The ambiguity latch needs explicit review/resume.
        self.executor.resume_new_risk()
        self.assertEqual(self.submit("new")["status"], "ack")

    def test_absent_unknown_order_is_not_proof_of_non_submission(self):
        self.broker.timeout_before_accept = True
        order = self.submit()
        self.reopen()
        self.assertFalse(self.executor.reconcile())
        self.assertEqual(self.submit()["status"], "unknown")
        self.assertEqual(self.broker.submit_count, 1)
        self.assertEqual(self.journal.order(order["client_id"])["status"], "unknown")

    def test_known_open_orders_remain_reserved_after_restart(self):
        order = self.submit(qty="8")
        self.reopen()
        with self.assertRaises(ExecutionError):
            self.submit("second", qty="8")
        self.assertTrue(self.executor.reconcile())
        self.assertEqual(self.journal.order(order["client_id"])["status"], "ack")
        with self.assertRaisesRegex(ExecutionError, "unhedged"):
            self.submit("second", qty="8")
        self.assertEqual(self.broker.submit_count, 1)

    def test_duplicate_and_out_of_order_fills_and_snapshots(self):
        order = self.submit()
        ack = self.broker.orders[order["client_id"]]
        partial = self.broker.fill(order["client_id"], Fill("newer-time", "1", "100", self.now + 1))
        self.executor.observe(partial)
        self.executor.observe(partial)
        full = self.broker.fill(order["client_id"], Fill("older-time", "1", "100", self.now))
        self.executor.observe(full)
        self.executor.observe(partial)
        self.executor.observe(ack)
        self.assertEqual(self.journal.order(order["client_id"])["status"], "filled")
        self.assertEqual(self.journal.positions()["lighter:QQQ"], 2)
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM execution_fills").fetchone()[0], 2)

    def test_conflicting_fill_identity_blocks_new_risk_without_corrupting_state(self):
        order = self.submit()
        partial = self.broker.fill(order["client_id"], Fill("id", "1", "100", self.now))
        self.executor.observe(partial)
        bad = replace(partial, fills=(Fill("id", "0.5", "100", self.now),))
        with self.assertRaises(ExecutionError):
            self.executor.observe(bad)
        self.assertEqual(self.journal.positions()["lighter:QQQ"], 1)
        self.assertEqual(self.journal.get("reconciled"), "0")

    def test_overfill_cumulative_jump_and_invalid_status_are_rejected(self):
        order = self.submit()
        ack = self.broker.orders[order["client_id"]]
        for bad in (replace(ack, cumulative_quantity="3", fills=(Fill("over", "3", "100", self.now),)),
                    replace(ack, cumulative_quantity="1"), replace(ack, cumulative_quantity="-1"),
                    replace(ack, status="filled"), replace(ack, status="partial")):
            with self.subTest(bad=bad), self.assertRaises(ExecutionError):
                self.executor.observe(bad)
        self.assertEqual(self.journal.positions(), {})
        self.assertEqual(self.journal.order(order["client_id"])["filled"], "0")

    def test_cancel_fill_race_records_fill_and_keeps_terminal_status(self):
        order = self.submit()
        partial = self.broker.fill(order["client_id"], Fill("before-cancel", "0.5", "100", self.now))
        self.executor.observe(partial)
        self.broker.cancel_fill = Fill("during-cancel", "0.4", "100", self.now + 1)
        result = self.executor.cancel(order["client_id"])
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(number(result["filled"]), number("0.9"))
        self.executor.observe(partial)
        self.assertEqual(self.journal.order(order["client_id"])["status"], "canceled")
        self.assertEqual(self.journal.positions()["lighter:QQQ"], number("0.9"))

    def test_cancel_unknown_followed_by_partial_does_not_enable_repeated_cancel(self):
        order = self.submit()
        partial = self.broker.fill(order["client_id"], Fill("partial-before-cancel", "0.5", "100", self.now))
        self.executor.observe(partial)
        self.broker.drop_next_cancel_ack = True
        result = self.executor.cancel(order["client_id"])
        self.assertEqual(result["status"], "unknown")
        stale_open = replace(self.broker.orders[order["client_id"]], status="ack")
        self.executor.observe(stale_open)
        self.assertEqual(self.journal.order(order["client_id"])["status"], "cancel_pending")
        self.executor.cancel(order["client_id"])
        self.assertEqual(self.broker.cancel_count, 1)
        self.reopen()
        self.assertTrue(self.executor.reconcile())
        self.assertEqual(self.journal.order(order["client_id"])["status"], "canceled")

    def test_reduce_only_can_reduce_a_position_already_over_exposure_limit(self):
        self.filled_position("10")
        self.market["lighter:QQQ"] = replace(self.market["lighter:QQQ"], mark="150")
        self.executor.limits = replace(self.limits, max_unhedged_notional_usdc="1000")
        self.assertEqual(self.submit("reduce-breach", "3", side="sell", reduce_only=True)["status"], "ack")
        with self.assertRaises(ExecutionError):
            self.submit("more-risk", "0.01")

    def test_reduce_only_pending_and_unknown_reserve_position_until_confirmation(self):
        self.filled_position()
        exit_order = self.submit("exit", "1.5", side="sell", reduce_only=True)
        self.broker.drop_next_cancel_ack = True
        self.executor.cancel(exit_order["client_id"])
        with self.assertRaisesRegex(ExecutionError, "unreserved"):
            self.submit("other-exit", "1", side="sell", reduce_only=True)
        self.reopen()
        with self.assertRaises(ExecutionError):
            self.submit("other-exit", "1", side="sell", reduce_only=True)
        self.assertTrue(self.executor.reconcile())
        self.assertEqual(self.submit("other-exit", "1", side="sell", reduce_only=True)["status"], "ack")

    def test_partial_reduce_fill_preserves_remaining_reservation(self):
        self.filled_position()
        exit_order = self.submit("exit", "1.5", side="sell", reduce_only=True)
        self.executor.observe(self.broker.fill(exit_order["client_id"], Fill("reduction", "0.5", "100", self.now)))
        with self.assertRaises(ExecutionError):
            self.submit("too-much", "0.51", side="sell", reduce_only=True)
        self.assertEqual(self.submit("remaining", "0.5", side="sell", reduce_only=True)["status"], "ack")

    def test_reduce_only_checks_direction_quantity_and_flat_position(self):
        with self.assertRaises(ExecutionError):
            self.submit("flat-exit", "1", side="sell", reduce_only=True)
        self.filled_position()
        for kwargs in ({"side": "buy", "qty": "1"}, {"side": "sell", "qty": "2.01"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ExecutionError):
                self.submit("bad-exit", reduce_only=True, **kwargs)
        with self.assertRaisesRegex(ExecutionError, "requires reduce_only"):
            self.submit("unmarked-exit", side="sell")

    def test_reducing_one_leg_cannot_bypass_unhedged_exposure_budget(self):
        self.filled_position("2")
        hedge = self.submit("hedge", "1", side="sell", venue="variational")
        self.executor.observe(self.broker.fill(hedge["client_id"], Fill("hedge-fill", "1", "200", self.now)))
        self.executor.limits = replace(self.limits, max_unhedged_notional_usdc="100")
        with self.assertRaisesRegex(ExecutionError, "unhedged"):
            self.submit("remove-hedge", "1", side="buy", venue="variational", reduce_only=True)

    def test_single_leg_rejection_latches_new_risk(self):
        self.filled_position()
        self.broker.reject_next = True
        self.assertEqual(self.submit("hedge", "1", side="sell", venue="variational")["status"], "rejected")
        with self.assertRaises(ExecutionError):
            self.submit("add-risk")
        self.assertEqual(self.submit("safe-exit", "1", side="sell", reduce_only=True)["status"], "ack")

    def test_market_authentication_and_margin_constraints(self):
        original = self.market["lighter:QQQ"]
        for state in (replace(original, observed_at=self.now - 6), replace(original, observed_at=self.now + 1),
                      replace(original, authenticated_at=self.now - 61), replace(original, authenticated=False),
                      replace(original, auth_expires_at=self.now), replace(original, available_margin_usdc="29"),
                      replace(original, mark="NaN"), replace(original, quantity_step="0")):
            self.market["lighter:QQQ"] = state
            with self.subTest(state=state), self.assertRaises(ExecutionError):
                self.submit()
        self.assertEqual(self.broker.submit_count, 0)

    def test_order_position_gross_and_unhedged_limits(self):
        for field in ("max_order_notional_usdc", "max_position_notional_usdc", "max_gross_notional_usdc", "max_unhedged_notional_usdc"):
            self.executor.limits = replace(self.limits, **{field: "199"})
            with self.subTest(field=field), self.assertRaises(ExecutionError):
                self.submit()
        self.assertEqual(self.broker.submit_count, 0)

    def test_pending_orders_consume_margin(self):
        self.market["lighter:QQQ"] = replace(self.market["lighter:QQQ"], available_margin_usdc="45")
        self.submit()
        with self.assertRaisesRegex(ExecutionError, "margin"):
            self.submit("another")

    def test_emergency_stop_persists_and_clearing_requires_reconciliation(self):
        self.filled_position()
        self.journal.emergency_stop()
        self.reopen()
        self.assertTrue(self.executor.reconcile())
        with self.assertRaises(ExecutionError):
            self.submit("blocked")
        self.assertEqual(self.submit("exit", "1", side="sell", reduce_only=True)["status"], "ack")
        self.journal.emergency_stop(False)
        with self.assertRaises(ExecutionError):
            self.executor.resume_new_risk()
        self.assertTrue(self.executor.reconcile())
        self.executor.resume_new_risk()

    def test_untracked_remote_order_and_position_mismatch_block_reconciliation(self):
        self.broker.submit("orphan", OrderSpec("lighter", "QQQ", "buy", "1", "100"))
        self.assertFalse(self.executor.reconcile())
        with self.assertRaises(ExecutionError):
            self.submit()
        self.broker.orders.clear()
        self.broker.positions["lighter:QQQ"] = number("1")
        self.assertFalse(self.executor.reconcile())

    def test_terminal_order_cannot_be_reopened_by_reconciliation(self):
        order = self.submit()
        self.executor.cancel(order["client_id"])
        self.broker.orders[order["client_id"]] = replace(self.broker.orders[order["client_id"]], status="ack")
        self.assertFalse(self.executor.reconcile())
        self.assertEqual(self.journal.order(order["client_id"])["status"], "canceled")

    def test_changed_broker_identity_and_payload_block_reconciliation(self):
        order = self.submit()
        old = self.broker.orders[order["client_id"]]
        for observed in (replace(old, broker_order_id="different"), replace(old, spec=replace(old.spec, quantity="3"))):
            self.broker.orders[order["client_id"]] = observed
            self.assertFalse(self.executor.reconcile())

    def test_non_fake_broker_is_rejected(self):
        with self.assertRaises(ExecutionError):
            SimulationExecutor(self.journal, object(), self.limits)

    def test_existing_paper_database_is_rejected_without_mutation(self):
        unrelated = Path(self.directory.name) / "paper.sqlite3"
        with closing(sqlite3.connect(unrelated)) as db:
            db.execute("CREATE TABLE positions(value TEXT)")
            db.execute("INSERT INTO positions VALUES ('preserve-me')")
            db.commit()
        before = unrelated.read_bytes()
        with self.assertRaises(ExecutionError):
            ExecutionJournal(unrelated)
        self.assertEqual(unrelated.read_bytes(), before)
        with closing(sqlite3.connect(unrelated)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("positions",)])
        self.assertFalse(Path(str(unrelated) + ".lock").exists())

    def test_existing_non_sqlite_file_is_rejected_without_mutation(self):
        unrelated = Path(self.directory.name) / "existing.txt"
        unrelated.write_bytes(b"preserve this unrelated file")
        before = unrelated.read_bytes()
        with self.assertRaises(ExecutionError):
            ExecutionJournal(unrelated)
        self.assertEqual(unrelated.read_bytes(), before)
        self.assertFalse(Path(str(unrelated) + ".lock").exists())


if __name__ == "__main__":
    unittest.main()
