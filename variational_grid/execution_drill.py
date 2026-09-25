"""Credential-free execution fault drill: python -m variational_grid.execution_drill."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from .execution import ExecutionError, ExecutionJournal, FakeBroker, Fill, MarketState, OrderSpec, RiskLimits, SimulationExecutor


def run_drill():
    now = 10000.0
    limits = RiskLimits("1000", "2000", "4000", "1500", "10", "0.1", 5, 60)
    market = {
        "lighter:QQQ": MarketState("100", now, now, now + 600, "1000", "0.01", "0.01"),
        "variational:US100S": MarketState("200", now, now, now + 600, "1000", "0.01", "0.01"),
    }
    entry = OrderSpec("lighter", "QQQ", "buy", "2", "100")
    checks = {}

    def blocked(action):
        try:
            action()
        except ExecutionError:
            return True
        return False

    with tempfile.TemporaryDirectory(prefix="variational-execution-drill-") as directory:
        path = Path(directory) / "journal.sqlite3"
        broker = FakeBroker()
        with ExecutionJournal(path) as journal:
            engine = SimulationExecutor(journal, broker, limits)
            checks["startup_requires_reconciliation"] = blocked(lambda: engine.submit("entry", entry, market, now))
            engine.reconcile()
            broker.drop_next_ack = True
            pending = engine.submit("entry", entry, market, now)
            checks["lost_ack_is_unknown"] = pending["status"] == "unknown"
            engine.submit("entry", entry, market, now)
            broker.fill(pending["client_id"], Fill("fill-a", "1", "100", now + 1))
            broker.fill(pending["client_id"], Fill("fill-b", "1", "100", now))
        with ExecutionJournal(path) as journal:
            engine = SimulationExecutor(journal, broker, limits)
            checks["restart_reconciles_without_resend"] = engine.reconcile() and broker.submit_count == 1
            engine.observe(broker.orders[pending["client_id"]])
            checks["duplicate_out_of_order_fills_are_idempotent"] = journal.positions()["lighter:QQQ"] == 2 and journal.db.execute("SELECT count(*) FROM execution_fills").fetchone()[0] == 2
            engine.resume_new_risk()
            exit_spec = OrderSpec("lighter", "QQQ", "sell", "1.5", "100", True)
            exit_order = engine.submit("exit", exit_spec, market, now)
            checks["pending_exit_reserves_position"] = blocked(lambda: engine.submit("extra-exit", replace(exit_spec, quantity="1"), market, now))
            broker.cancel_fill = Fill("cancel-race", "0.5", "100", now + 2)
            broker.drop_next_cancel_ack = True
            canceled = engine.cancel(exit_order["client_id"])
            checks["lost_cancel_ack_stays_unknown"] = canceled["status"] == "unknown"
            checks["cancel_race_reconciles_fill"] = engine.reconcile() and journal.positions()["lighter:QQQ"] == 1.5
            engine.resume_new_risk()
            broker.reject_next = True
            hedge = OrderSpec("variational", "US100S", "sell", "0.5", "200")
            engine.submit("hedge", hedge, market, now)
            checks["failed_leg_blocks_new_risk"] = blocked(lambda: engine.submit("blocked-entry", entry, market, now))
            journal.emergency_stop()
        with ExecutionJournal(path) as journal:
            engine = SimulationExecutor(journal, broker, limits)
            engine.reconcile()
            checks["emergency_stop_survives_restart"] = journal.get("emergency_stop") == "1" and blocked(lambda: engine.submit("stopped", entry, market, now))
            safe_exit = OrderSpec("lighter", "QQQ", "sell", "0.5", "100", True)
            stale = {**market, "lighter:QQQ": replace(market["lighter:QQQ"], observed_at=now - 6)}
            expired = {**market, "lighter:QQQ": replace(market["lighter:QQQ"], auth_expires_at=now)}
            checks["stale_market_blocks_intents"] = blocked(lambda: engine.submit("stale", safe_exit, stale, now))
            checks["expired_auth_blocks_intents"] = blocked(lambda: engine.submit("expired", safe_exit, expired, now))
            checks["over_reduction_is_rejected"] = blocked(lambda: engine.submit("over-exit", replace(safe_exit, quantity="2"), market, now))
        with ExecutionJournal(Path(directory) / "absent.sqlite3") as journal:
            broker = FakeBroker()
            engine = SimulationExecutor(journal, broker, limits)
            engine.reconcile()
            broker.timeout_before_accept = True
            engine.submit("absent", entry, market, now)
            checks["absent_unknown_order_is_not_resent"] = not engine.reconcile() and engine.submit("absent", entry, market, now)["status"] == "unknown" and broker.submit_count == 1
    return {"live_enabled": False, "broker": "in_memory_fake", "risk_budget_source": "drill_examples_only",
            "temporary_storage_removed": True, "passed": all(checks.values()), "checks": checks}


def main():
    result = run_drill()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
