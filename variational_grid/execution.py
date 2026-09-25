"""Offline execution safety rehearsal. This module cannot enable live trading."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid

from .store import ProcessLock


LIVE_ENABLED = False
TERMINAL = {"canceled", "filled", "rejected"}
STATES = TERMINAL | {"sending", "ack", "partial", "cancel_pending", "unknown"}


class ExecutionError(Exception):
    pass


def number(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal("1e24"):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise ExecutionError("Invalid finite simulation number") from None


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ExecutionError("Invalid observation time")
    return float(value)


@dataclass(frozen=True)
class OrderSpec:
    venue: str
    symbol: str
    side: str
    quantity: str
    limit_price: str
    reduce_only: bool = False

    @property
    def asset(self):
        return self.venue + ":" + self.symbol

    @property
    def sign(self):
        return Decimal(1 if self.side == "buy" else -1)

    def normalized(self):
        if {"lighter": "QQQ", "variational": "US100S"}.get(self.venue) != self.symbol:
            raise ExecutionError("Only the two simulation instruments are supported")
        if self.side not in {"buy", "sell"} or type(self.reduce_only) is not bool:
            raise ExecutionError("Invalid order direction or reduce-only flag")
        qty, price = number(self.quantity), number(self.limit_price)
        if qty <= 0 or price <= 0:
            raise ExecutionError("Quantity and price must be positive")
        return OrderSpec(self.venue, self.symbol, self.side, str(qty.normalize()), str(price.normalize()), self.reduce_only)


@dataclass(frozen=True)
class MarketState:
    mark: str
    observed_at: float
    authenticated_at: float
    auth_expires_at: float
    available_margin_usdc: str
    quantity_step: str
    price_tick: str
    authenticated: bool = True


@dataclass(frozen=True)
class RiskLimits:
    """All money limits are mandatory simulation inputs, not live defaults."""
    max_order_notional_usdc: str
    max_position_notional_usdc: str
    max_gross_notional_usdc: str
    max_unhedged_notional_usdc: str
    min_free_margin_usdc: str
    initial_margin_fraction: str
    max_market_age_seconds: float
    max_auth_age_seconds: float

    def validate(self):
        for name in ("max_order_notional_usdc", "max_position_notional_usdc", "max_gross_notional_usdc", "max_unhedged_notional_usdc"):
            if number(getattr(self, name)) <= 0:
                raise ExecutionError("Risk budgets must be explicitly positive")
        if number(self.min_free_margin_usdc) < 0 or not 0 < number(self.initial_margin_fraction) <= 1:
            raise ExecutionError("Invalid simulation margin policy")
        for value in (self.max_market_age_seconds, self.max_auth_age_seconds):
            timestamp(value)
        return self


@dataclass(frozen=True)
class Fill:
    trade_id: str
    quantity: str
    price: str
    ts: float


@dataclass(frozen=True)
class OrderObservation:
    client_id: str
    broker_order_id: str
    spec: OrderSpec
    status: str
    cumulative_quantity: str
    fills: tuple[Fill, ...] = ()


class ExecutionJournal:
    """Single writer journal, acquired before any broker interaction."""
    def __init__(self, path):
        self.path = Path(path)
        self._check_existing()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = ProcessLock(self.path)
        self.lock.__enter__()
        self.db = None
        try:
            self._check_existing()
            self.db = sqlite3.connect(self.path, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS execution_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_intents(
                    intent_key TEXT PRIMARY KEY,client_id TEXT UNIQUE NOT NULL,spec TEXT NOT NULL,
                    status TEXT NOT NULL,filled TEXT NOT NULL,broker_order_id TEXT,created REAL NOT NULL,reason TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS execution_fills(
                    venue TEXT NOT NULL,trade_id TEXT NOT NULL,client_id TEXT NOT NULL,quantity TEXT NOT NULL,
                    price TEXT NOT NULL,ts REAL NOT NULL,PRIMARY KEY(venue,trade_id));
                CREATE TABLE IF NOT EXISTS execution_positions(asset TEXT PRIMARY KEY,quantity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL);
            """)
            with self.transaction():
                version = self.get("format")
                if version not in (None, "offline-execution-v1"):
                    raise ExecutionError("Unsupported simulation journal")
                self.set("format", "offline-execution-v1")
                if self.get("namespace") is None:
                    self.set("namespace", uuid.uuid4().hex)
                    self.set("emergency_stop", "0")
                    self.set("risk_latch", "")
                self.set("reconciled", "0")
                self.event("startup", {"live_enabled": False})
        except BaseException:
            self.close()
            raise

    def _check_existing(self):
        if not self.path.exists() or self.path.is_file() and self.path.stat().st_size == 0:
            return
        expected = {"execution_meta", "execution_intents", "execution_fills", "execution_positions", "execution_events"}
        try:
            # No schema creation or journal-mode change against an unrelated file.
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables != expected:
                    raise ExecutionError("Existing file is not an isolated execution journal")
                row = connection.execute("SELECT value FROM execution_meta WHERE key='format'").fetchone()
                if row is None or row[0] != "offline-execution-v1":
                    raise ExecutionError("Existing file has no supported execution format")
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            raise ExecutionError("Existing file is not a readable execution journal") from None

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, key):
        row = self.db.execute("SELECT value FROM execution_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.db.execute("INSERT INTO execution_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def event(self, kind, payload):
        self.db.execute("INSERT INTO execution_events(kind,payload) VALUES (?,?)", (kind, json.dumps(payload, sort_keys=True)))

    def orders(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM execution_intents ORDER BY created,client_id")]

    def order(self, client_id):
        row = self.db.execute("SELECT * FROM execution_intents WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            raise ExecutionError("Unknown local client id")
        return dict(row)

    def positions(self):
        return {row[0]: number(row[1]) for row in self.db.execute("SELECT asset,quantity FROM execution_positions")}

    def latch(self, reason, *, invalidate=False):
        with self.transaction():
            self.set("risk_latch", reason)
            if invalidate:
                self.set("reconciled", "0")
            self.event("risk_latch", {"reason": reason})

    def emergency_stop(self, enabled=True):
        if type(enabled) is not bool:
            raise ExecutionError("Emergency stop requires an explicit boolean")
        with self.transaction():
            self.set("emergency_stop", "1" if enabled else "0")
            if not enabled:
                self.set("reconciled", "0")
            self.event("emergency_stop", {"enabled": enabled})

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            self.lock.__exit__()
            self.lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _spec(row):
    return OrderSpec(**json.loads(row["spec"])).normalized()


class SimulationExecutor:
    live_enabled = False

    def __init__(self, journal, broker, limits):
        # No pluggable live adapter: adding one requires a separately reviewed integration.
        if type(broker) is not FakeBroker:
            raise ExecutionError("Only the built-in offline FakeBroker is accepted")
        self.journal, self.broker, self.limits = journal, broker, limits.validate()

    def _risk_check(self, spec, market, now):
        journal, policy = self.journal, self.limits
        if journal.get("reconciled") != "1":
            raise ExecutionError("Reconcile before any new intent")
        if not spec.reduce_only and (journal.get("emergency_stop") == "1" or journal.get("risk_latch")):
            raise ExecutionError("New risk is stopped; reconcile and explicitly resume")
        positions = journal.positions()
        active = [row for row in journal.orders() if row["status"] not in TERMINAL]
        if not spec.reduce_only and any(row["status"] in {"unknown", "sending"} for row in active):
            raise ExecutionError("Unknown order blocks new risk")
        assets = {spec.asset} | {a for a, q in positions.items() if q} | {_spec(row).asset for row in active}
        for asset in assets:
            state = market.get(asset)
            if not isinstance(state, MarketState):
                raise ExecutionError("Missing market or account observation")
            if not 0 <= now - timestamp(state.observed_at) <= policy.max_market_age_seconds:
                raise ExecutionError("Stale or future market observation")
            if (state.authenticated is not True or not 0 <= now - timestamp(state.authenticated_at) <= policy.max_auth_age_seconds
                    or timestamp(state.auth_expires_at) <= now):
                raise ExecutionError("Authentication observation is stale or expired")
            if any(number(value) <= 0 for value in (state.mark, state.quantity_step, state.price_tick)) or number(state.available_margin_usdc) < 0:
                raise ExecutionError("Invalid market increments or margin")
        state, qty, price = market[spec.asset], number(spec.quantity), number(spec.limit_price)
        if qty % number(state.quantity_step) or price % number(state.price_tick):
            raise ExecutionError("Order is off the simulation quantity or price step")
        current = positions.get(spec.asset, Decimal(0))
        if spec.reduce_only:
            reserved = sum((number(_spec(row).quantity) - number(row["filled"]) for row in active
                            if _spec(row).asset == spec.asset and _spec(row).reduce_only), Decimal(0))
            if current == 0 or current * spec.sign >= 0 or qty + reserved > abs(current):
                raise ExecutionError("Reduce-only quantity exceeds unreserved confirmed position")
        elif current * spec.sign < 0:
            raise ExecutionError("Closing an existing position requires reduce_only")
        nonreducing = [(_spec(row), number(_spec(row).quantity) - number(row["filled"])) for row in active if not _spec(row).reduce_only]
        if not spec.reduce_only and any(order.asset == spec.asset and order.side != spec.side for order, _ in nonreducing):
            raise ExecutionError("Opposing unresolved opening intent")
        notional = qty * max(price, number(state.mark))
        if notional > number(policy.max_order_notional_usdc):
            raise ExecutionError("Order notional limit")
        net = sum((q * number(market[a].mark) for a, q in positions.items() if q), Decimal(0))
        # Either leg can fill alone. Pending exits can also remove a hedge.
        possible = [(_spec(row), (number(_spec(row).quantity) - number(row["filled"])) * number(market[_spec(row).asset].mark)) for row in active]
        buys = sum((n for order, n in possible if order.side == "buy"), Decimal(0))
        sells = sum((n for order, n in possible if order.side == "sell"), Decimal(0))
        before_worst = max(abs(net + buys), abs(net - sells))
        if spec.side == "buy":
            buys += qty * number(state.mark)
        else:
            sells += qty * number(state.mark)
        ceiling = number(policy.max_unhedged_notional_usdc)
        if spec.reduce_only:
            ceiling = max(ceiling, before_worst)
        if max(abs(net + buys), abs(net - sells)) > ceiling:
            raise ExecutionError("Worst-case unhedged exposure limit")
        if spec.reduce_only:
            return  # Does not reserve new initial margin or increase gross position.
        reservations = [(order, remaining * max(number(order.limit_price), number(market[order.asset].mark))) for order, remaining in nonreducing]
        per_asset = abs(current) * number(state.mark) + notional + sum((n for order, n in reservations if order.asset == spec.asset), Decimal(0))
        if per_asset > number(policy.max_position_notional_usdc):
            raise ExecutionError("Position notional limit including pending orders")
        gross = sum((abs(q) * number(market[a].mark) for a, q in positions.items() if q), Decimal(0)) + notional + sum((n for _, n in reservations), Decimal(0))
        if gross > number(policy.max_gross_notional_usdc):
            raise ExecutionError("Gross position and order limit")
        reservations.append((spec, notional))
        venue_pending = sum((n for order, n in reservations if order.venue == spec.venue), Decimal(0))
        if number(state.available_margin_usdc) - venue_pending * number(policy.initial_margin_fraction) < number(policy.min_free_margin_usdc):
            raise ExecutionError("Available margin does not cover local pending reservations")

    def submit(self, intent_key, spec, market, now):
        now, spec = timestamp(now), spec.normalized()
        if not isinstance(intent_key, str) or not 0 < len(intent_key) <= 128:
            raise ExecutionError("A stable, nonempty intent key is required")
        encoded = json.dumps(asdict(spec), sort_keys=True)
        with self.journal.transaction():
            existing = self.journal.db.execute("SELECT * FROM execution_intents WHERE intent_key=?", (intent_key,)).fetchone()
            if existing is not None:
                if existing["spec"] != encoded:
                    raise ExecutionError("Intent key already belongs to a different order")
                return dict(existing)  # Never retry a sending/unknown intent.
            self._risk_check(spec, market, now)
            client_id = "sim_" + hashlib.sha256((self.journal.get("namespace") + ":" + intent_key).encode()).hexdigest()[:28]
            self.journal.db.execute("INSERT INTO execution_intents VALUES (?,?,?,?,?,?,?,?,0)", (intent_key, client_id, encoded, "sending", "0", None, now, ""))
            self.journal.event("send_intent", {"client_id": client_id, "live_enabled": False})
        # The intent commit, including its stable client id, precedes the broker call.
        try:
            observation = self.broker.submit(client_id, spec)
        except Exception:
            self._unknown(client_id, "Submission outcome unknown; query only, never resend")
            return self.journal.order(client_id)
        self.observe(observation)
        return self.journal.order(client_id)

    def _unknown(self, client_id, reason):
        with self.journal.transaction():
            self.journal.db.execute("UPDATE execution_intents SET status='unknown',reason=? WHERE client_id=?", (reason, client_id))
            self.journal.set("risk_latch", reason)
            self.journal.event("unknown", {"client_id": client_id})

    def cancel(self, client_id):
        row = self.journal.order(client_id)
        if row["status"] in TERMINAL or row["status"] in {"unknown", "sending", "cancel_pending"}:
            return row  # Resolve ambiguous cancels by observation, never by retry.
        if self.journal.get("reconciled") != "1":
            raise ExecutionError("Reconcile before requesting cancellation")
        with self.journal.transaction():
            self.journal.db.execute("UPDATE execution_intents SET status='cancel_pending',cancel_requested=1 WHERE client_id=?", (client_id,))
            self.journal.event("cancel_intent", {"client_id": client_id})
        try:
            observation = self.broker.cancel(client_id)
        except Exception:
            self._unknown(client_id, "Cancellation outcome unknown; remaining quantity stays reserved")
            return self.journal.order(client_id)
        self.observe(observation)
        return self.journal.order(client_id)

    def observe(self, observation):
        try:
            with self.journal.transaction():
                self._observe(observation)
        except (ExecutionError, TypeError, ValueError, KeyError, AttributeError):
            self.journal.latch("Broker observation mismatch; reconciliation required", invalidate=True)
            raise ExecutionError("Broker observation mismatch; reconciliation required") from None

    def _observe(self, observation):
        if not isinstance(observation, OrderObservation) or observation.status not in STATES - {"sending", "unknown"}:
            raise ExecutionError("Unsupported broker observation")
        row = self.journal.order(observation.client_id)
        spec = _spec(row)
        if observation.spec.normalized() != spec or not isinstance(observation.broker_order_id, str) or not observation.broker_order_id:
            raise ExecutionError("Order identity mismatch")
        if row["broker_order_id"] and row["broker_order_id"] != observation.broker_order_id:
            raise ExecutionError("Broker order id changed")
        prior, total = number(row["filled"]), number(row["filled"])
        for fill in observation.fills:
            if not isinstance(fill, Fill) or not isinstance(fill.trade_id, str) or not 0 < len(fill.trade_id) <= 128:
                raise ExecutionError("Invalid execution identity")
            qty, price, ts = number(fill.quantity), number(fill.price), timestamp(fill.ts)
            if qty <= 0 or price <= 0 or (spec.side == "buy" and price > number(spec.limit_price)) or (spec.side == "sell" and price < number(spec.limit_price)):
                raise ExecutionError("Execution exceeds order limit")
            old = self.journal.db.execute("SELECT client_id,quantity,price,ts FROM execution_fills WHERE venue=? AND trade_id=?", (spec.venue, fill.trade_id)).fetchone()
            if old:
                if old[0] != row["client_id"] or number(old[1]) != qty or number(old[2]) != price or old[3] != ts:
                    raise ExecutionError("Execution id reused with different content")
                continue
            total += qty
            if total > number(spec.quantity) or row["status"] == "rejected":
                raise ExecutionError("Execution exceeds order quantity")
            position = self.journal.positions().get(spec.asset, Decimal(0))
            if spec.reduce_only and (position * spec.sign >= 0 or qty > abs(position)):
                raise ExecutionError("Execution would reverse a reduce-only position")
            self.journal.db.execute("INSERT INTO execution_fills VALUES (?,?,?,?,?,?)", (spec.venue, fill.trade_id, row["client_id"], str(qty), str(price), ts))
            self.journal.db.execute("INSERT INTO execution_positions VALUES (?,?) ON CONFLICT(asset) DO UPDATE SET quantity=excluded.quantity", (spec.asset, str(position + qty * spec.sign)))
        cumulative = number(observation.cumulative_quantity)
        if not 0 <= cumulative <= number(spec.quantity):
            raise ExecutionError("Invalid broker cumulative quantity")
        if cumulative < prior and total == prior:
            self.journal.event("stale_observation", {"client_id": row["client_id"]})
            return  # An older snapshot cannot undo known fills or terminal state.
        if cumulative != total or not 0 <= total <= number(spec.quantity):
            raise ExecutionError("Cumulative execution does not match identified fills")
        state = observation.status
        if (state == "filled" and total != number(spec.quantity)) or (state == "rejected" and total != 0) or (state == "partial" and not 0 < total < number(spec.quantity)):
            raise ExecutionError("Order status and execution quantity disagree")
        if total == number(spec.quantity):
            state = "filled"
        elif row["status"] in TERMINAL:
            state = row["status"]
        elif state == "ack" and total:
            state = "partial"
        if row["cancel_requested"] and state in {"ack", "partial"}:
            state = "cancel_pending"
        self.journal.db.execute("UPDATE execution_intents SET status=?,filled=?,broker_order_id=?,reason='' WHERE client_id=?", (state, str(total), observation.broker_order_id, row["client_id"]))
        if state == "rejected":
            self.journal.set("risk_latch", "A leg was rejected; inspect exposure before resuming")
        self.journal.event("observation", {"client_id": row["client_id"], "status": state, "filled": str(total)})

    def reconcile(self):
        with self.journal.transaction():
            self.journal.set("reconciled", "0")
        try:
            observations, broker_positions = self.broker.snapshot()
            indexed = {item.client_id: item for item in observations}
            local = self.journal.orders()
            if len(indexed) != len(observations) or set(indexed) != {row["client_id"] for row in local}:
                raise ExecutionError("Missing or untracked broker order; do not resend")
            for row in local:
                self.observe(indexed[row["client_id"]])
                saved = self.journal.order(row["client_id"])
                remote = indexed[row["client_id"]]
                if saved["status"] in TERMINAL and remote.status not in TERMINAL:
                    raise ExecutionError("Broker reopened a terminal local order")
                if number(saved["filled"]) != number(remote.cumulative_quantity):
                    raise ExecutionError("Reconciliation returned an older cumulative quantity")
            expected = {key: number(value) for key, value in broker_positions.items() if number(value)}
            actual = {key: value for key, value in self.journal.positions().items() if value}
            if expected != actual or any(row["status"] in {"unknown", "sending"} for row in self.journal.orders()):
                raise ExecutionError("Position or order reconciliation mismatch")
        except Exception:
            self.journal.latch("Reconciliation incomplete; new risk disabled", invalidate=True)
            return False
        with self.journal.transaction():
            self.journal.set("reconciled", "1")
            self.journal.event("reconciled", {"orders": len(local)})
        return True

    def resume_new_risk(self):
        if self.journal.get("reconciled") != "1" or self.journal.get("emergency_stop") == "1":
            raise ExecutionError("A reconciled account and cleared emergency stop are required")
        if any(row["status"] in {"unknown", "sending"} for row in self.journal.orders()):
            raise ExecutionError("Unknown orders still exist")
        with self.journal.transaction():
            self.journal.set("risk_latch", "")
            self.journal.event("explicit_resume", {"live_enabled": False})


class FakeBroker:
    """In-memory exchange fixture; retained across executor restarts in the drill."""
    live_enabled = False

    def __init__(self):
        self.orders = {}
        self.positions = {}
        self.submit_count = 0
        self.cancel_count = 0
        self.drop_next_ack = False
        self.reject_next = False
        self.timeout_before_accept = False
        self.drop_next_cancel_ack = False
        self.cancel_fill = None

    def submit(self, client_id, spec):
        self.submit_count += 1
        if self.timeout_before_accept:
            self.timeout_before_accept = False
            raise TimeoutError("Injected transport uncertainty before acceptance")
        if client_id in self.orders:
            if self.orders[client_id].spec != spec:
                raise ExecutionError("Fake broker client id collision")
            return self.orders[client_id]
        status = "rejected" if self.reject_next else "ack"
        self.reject_next = False
        observation = OrderObservation(client_id, "fake-" + client_id, spec, status, "0")
        self.orders[client_id] = observation
        if self.drop_next_ack:
            self.drop_next_ack = False
            raise TimeoutError("Injected accepted order with lost acknowledgment")
        return observation

    def fill(self, client_id, fill):
        order = self.orders[client_id]
        for other in self.orders.values():
            if other.spec.venue == order.spec.venue:
                for previous in other.fills:
                    if previous.trade_id == fill.trade_id:
                        if other.client_id != client_id or previous != fill:
                            raise ExecutionError("Conflicting fake execution id")
                        return order
        if order.status in TERMINAL:
            raise ExecutionError("Cannot newly fill a terminal fake order")
        total = number(order.cumulative_quantity) + number(fill.quantity)
        if number(fill.quantity) <= 0 or total > number(order.spec.quantity):
            raise ExecutionError("Invalid fake execution quantity")
        status = "filled" if total == number(order.spec.quantity) else "partial"
        result = OrderObservation(client_id, order.broker_order_id, order.spec, status, str(total), order.fills + (fill,))
        self.orders[client_id] = result
        self.positions[order.spec.asset] = self.positions.get(order.spec.asset, Decimal(0)) + number(fill.quantity) * order.spec.sign
        return result

    def cancel(self, client_id):
        self.cancel_count += 1
        if self.cancel_fill is not None:
            fill, self.cancel_fill = self.cancel_fill, None
            self.fill(client_id, fill)
        old = self.orders[client_id]
        if old.status not in TERMINAL:
            old = OrderObservation(client_id, old.broker_order_id, old.spec, "canceled", old.cumulative_quantity, old.fills)
            self.orders[client_id] = old
        if self.drop_next_cancel_ack:
            self.drop_next_cancel_ack = False
            raise TimeoutError("Injected lost cancellation acknowledgment")
        return old

    def snapshot(self):
        return tuple(self.orders.values()), dict(self.positions)
