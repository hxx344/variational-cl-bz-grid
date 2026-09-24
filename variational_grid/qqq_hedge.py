"""Deterministic long-only maker ladder and dollar-delta hedge, PAPER ONLY."""
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import sqlite3

from .models import D, GridError, dec
from .store import Store


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def floor(value, step):
    return (dec(value) / dec(step)).to_integral_value(rounding=ROUND_FLOOR) * dec(step)


@dataclass(frozen=True)
class QQQSettings:
    grid_count: int = 30
    order_notional_usdc: str = "1000"
    poll_seconds: float = 2
    maker_latency_ms: int = 200
    cancel_latency_ms: int = 300
    queue_multiplier: str = "1"
    beta: str = "1"
    hedge_reset_fraction: str = "0.5"
    lighter_fee_bps: str = "0"
    var_fee_bps: str = "0"
    var_slippage_bps: str = "1"
    max_quote_age_seconds: float = 15
    max_pair_skew_seconds: float = 10

    def validate(self):
        if type(self.grid_count) is not int or not 1 <= self.grid_count <= 200:
            raise GridError("grid_count must be an integer in [1, 200]")
        if not 1 <= dec(self.poll_seconds) <= 60 or not 1 <= dec(self.max_quote_age_seconds) <= 120:
            raise GridError("Invalid QQQ polling or quote age")
        if not 0 < dec(self.max_pair_skew_seconds) <= dec(self.max_quote_age_seconds):
            raise GridError("Invalid QQQ pair skew")
        for name in ("maker_latency_ms", "cancel_latency_ms"):
            if type(getattr(self, name)) is not int or not 0 <= getattr(self, name) <= 60000:
                raise GridError("Invalid maker/cancel latency")
        if dec(self.order_notional_usdc) <= 0 or not 0 < dec(self.beta) <= 10:
            raise GridError("Invalid QQQ order notional or dollar beta")
        if not 0 <= dec(self.hedge_reset_fraction) < 1 or dec(self.queue_multiplier) < 1:
            raise GridError("Invalid reset fraction or conservative queue multiplier")
        for name in ("lighter_fee_bps", "var_fee_bps", "var_slippage_bps"):
            if not 0 <= dec(getattr(self, name)) <= 100:
                raise GridError("Invalid fee or slippage bps")
        return self


@dataclass(frozen=True)
class QQQConfig:
    settings: QQQSettings
    name: str
    grid_step_percent: str
    hedge_tolerance_percent: str
    state_file: str

    def strategy_identity(self):
        return encoded({"model_version": 1, "settings": asdict(self.settings), "name": self.name,
                        "grid_step_percent": self.grid_step_percent, "hedge_tolerance_percent": self.hedge_tolerance_percent,
                        "mapping": {"lighter": "QQQ", "var": "US100S", "multipliers": "1"},
                        "pnl_basis": "before_funding_and_dividends"})


def initial_account():
    def leg():
        return {"qty": "0", "average_entry": "0", "realized_gross": "0", "fees_usdc": "0",
                "volume_units": "0", "turnover_usdc": "0", "fill_count": 0}
    return {"qqq": leg(), "us100": leg(), "anchor": None, "slots": [], "orders": [],
            "next_order": 1, "next_fill": 1, "peak_pnl": "0", "max_drawdown_usdc": "0",
            "last_ts": None, "last_var_mark": None, "last_var_ts": None, "gap_count": 0,
            "hedge_adjustments": 0, "recenter_pending": False}


class QQQStore:
    get, set, transaction, snapshot, close = Store.get, Store.set, Store.transaction, Store.snapshot, Store.close

    def __init__(self, path, config):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS ticks(id INTEGER PRIMARY KEY,ts REAL UNIQUE,snapshot TEXT NOT NULL,account TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,frame_ts REAL NOT NULL,payload TEXT NOT NULL);")
        identity = self.get("config")
        if identity is not None and identity != config.strategy_identity():
            self.close()
            raise GridError("QQQ economics changed; use a new output_dir")
        with self.transaction():
            self.set("config", config.strategy_identity())
            if self.get("account") is None:
                self.set("account", encoded(initial_account()))

    def account(self):
        return json.loads(self.get("account"))

    def reset(self, config):
        with self.transaction():
            for table in ("ticks", "fills", "meta"):
                self.db.execute("DELETE FROM " + table)
            self.set("config", config.strategy_identity())
            self.set("account", encoded(initial_account()))


def book_fill(account, venue, signed_qty, price, fee_bps, ts, reason, slot=None):
    """Average-cost linear accounting; execution slippage is already in price."""
    leg = account[venue]
    old, avg, change, price = dec(leg["qty"]), dec(leg["average_entry"]), dec(signed_qty), dec(price)
    if not change or price <= 0:
        raise GridError("Invalid simulated fill")
    new = old + change
    realized = D(0)
    if old * change >= 0:
        avg = (abs(old) * avg + abs(change) * price) / abs(new)
    else:
        closed = min(abs(old), abs(change))
        realized = closed * (price - avg) * (1 if old > 0 else -1)
        if not new:
            avg = D(0)
        elif old * new < 0:
            avg = price
    fee = abs(change) * price * dec(fee_bps) / 10000
    leg.update(qty=str(new), average_entry=str(avg), realized_gross=str(dec(leg["realized_gross"]) + realized),
               fees_usdc=str(dec(leg["fees_usdc"]) + fee), volume_units=str(dec(leg["volume_units"]) + abs(change)),
               turnover_usdc=str(dec(leg["turnover_usdc"]) + abs(change) * price), fill_count=leg["fill_count"] + 1)
    fill = {"id": account["next_fill"], "ts": ts, "venue": "Lighter" if venue == "qqq" else "Variational",
            "symbol": "QQQ" if venue == "qqq" else "US100", "side": "buy" if change > 0 else "sell",
            "qty": str(abs(change)), "price": str(price), "fee": str(fee), "notional": str(abs(change) * price),
            "reason": reason, "slot": slot, "realized_gross": str(realized)}
    account["next_fill"] += 1
    return fill


def exposure(account, qqq_mark, var_mark, beta):
    a = dec(account["qqq"]["qty"]) * dec(qqq_mark) * dec(beta)
    b = dec(account["us100"]["qty"]) * dec(var_mark)
    gross = abs(a) + abs(b)
    return a + b, gross, (a + b) / gross if gross else D(0)


def hedge_target(account, qqq_mark, var_mark, config, size_step):
    """Solve net/gross = signed half-band, adjusting only the US100 leg."""
    net, gross, ratio = exposure(account, qqq_mark, var_mark, config.settings.beta)
    old, step = dec(account["us100"]["qty"]), dec(size_step)
    threshold = dec(config.hedge_tolerance_percent) / 100
    if not gross or abs(ratio) <= threshold:
        return old
    a = dec(account["qqq"]["qty"]) * dec(qqq_mark) * dec(config.settings.beta)
    if not a:
        return D(0)
    target_ratio = (1 if net > 0 else -1) * threshold * dec(config.settings.hedge_reset_fraction)
    ideal = -a * (1 - target_ratio) / (1 + target_ratio) / dec(var_mark)
    candidates = {floor(ideal, step), floor(ideal, step) + step}
    def score(quantity):
        b = quantity * dec(var_mark)
        r = (a + b) / (a + abs(b))
        return abs(r - target_ratio), abs(quantity - old)
    return min(candidates, key=score)


def _order(account, slot, side, qty, price, market, now, settings):
    # A post-only order crossing the observed book is rejected and retried later.
    if (side == "buy" and price >= dec(market["ask"])) or (side == "sell" and price <= dec(market["bid"])):
        return
    if qty < dec(market["min_qty"]) or qty * price < dec(market.get("min_notional", "0")):
        return
    depth = market["bids" if side == "buy" else "asks"]
    covered = depth and (price >= min(dec(p) for p, _ in depth) if side == "buy" else price <= max(dec(p) for p, _ in depth))
    queue = sum((dec(q) for p, q in depth if dec(p) == price), D(0)) * dec(settings.queue_multiplier) if covered else None
    account["orders"].append({"id": account["next_order"], "slot": slot["slot"], "side": side,
                              "price": str(price), "remaining": str(qty), "queue": str(queue) if queue is not None else None,
                              "active_ts": now + settings.maker_latency_ms / 1000, "cancel_ts": None})
    account["next_order"] += 1


def maker_step(before, market, now, config, allow_entries):
    """Consume each observed trade at most once per account, including partial fills."""
    account = json.loads(encoded(before))
    settings, fills = config.settings, []
    slots = {x["slot"]: x for x in account["slots"]}
    if market["gap"] or not market["ready"]:
        # Missing flow cannot establish fill or priority. Drop simulated pending orders,
        # retain all known inventory, then requeue from a later complete observation.
        account["orders"] = []
        account["gap_count"] += int(market["gap"])
        return account, fills
    for trade in market["trades"]:
        ts, price, volume = trade["ts"], dec(trade["price"]), dec(trade["qty"])
        side = "buy" if trade["side"] == "sell" else "sell"
        orders = sorted((o for o in account["orders"] if o["side"] == side),
                        key=lambda o: ((-1 if side == "buy" else 1) * dec(o["price"]), o["id"]))
        for order in orders:
            limit = dec(order["price"])
            if order["queue"] is None or not volume or ts < order["active_ts"] or (order["cancel_ts"] is not None and ts >= order["cancel_ts"]):
                continue
            if (side == "buy" and price > limit) or (side == "sell" and price < limit):
                continue
            queue = dec(order["queue"]) if price == limit else D(0)
            used = min(queue, volume)
            order["queue"], volume = str(queue - used), volume - used
            amount = min(volume, dec(order["remaining"]))
            slot = slots[order["slot"]]
            if side == "sell":
                amount = min(amount, dec(slot["qty"]))
            amount = floor(amount, market["size_step"])
            if not amount:
                continue
            order["remaining"] = str(dec(order["remaining"]) - amount)
            volume -= amount
            delta = amount if side == "buy" else -amount
            fills.append(book_fill(account, "qqq", delta, limit, settings.lighter_fee_bps, ts,
                                   "maker_entry" if side == "buy" else "maker_take_profit", slot["slot"]))
            slot["qty"] = str(dec(slot["qty"]) + delta)
            if side == "buy":
                slot["entered"] = str(dec(slot["entered"]) + amount)
    account["orders"] = [o for o in account["orders"] if dec(o["remaining"]) > 0 and
                         (o["cancel_ts"] is None or o["cancel_ts"] > now)]
    for order in account["orders"]:
        if order["queue"] is None:
            price = dec(order["price"])
            depth = market["bids" if order["side"] == "buy" else "asks"]
            covered = depth and (price >= min(dec(p) for p, _ in depth) if order["side"] == "buy" else price <= max(dec(p) for p, _ in depth))
            if covered:
                order["queue"] = str(sum((dec(q) for p, q in depth if dec(p) == price), D(0)) * dec(settings.queue_multiplier))
                order["active_ts"] = now + settings.maker_latency_ms / 1000
    if not allow_entries:
        for order in account["orders"]:
            if order["side"] == "buy" and order["cancel_ts"] is None:
                order["cancel_ts"] = now + settings.cancel_latency_ms / 1000
    if now - market["ts"] > settings.max_quote_age_seconds:
        return account, fills  # Preserve already observed fills, defer new order decisions.
    step = dec(config.grid_step_percent) / 100
    mid, tick = (dec(market["bid"]) + dec(market["ask"])) / 2, dec(market["price_tick"])
    flat = not dec(account["qqq"]["qty"])
    if flat and account["anchor"] is not None and mid > dec(account["anchor"]) * (1 + step):
        account["recenter_pending"] = True
        for o in account["orders"]:
            if o["cancel_ts"] is None:
                o["cancel_ts"] = now + settings.cancel_latency_ms / 1000
    if flat and not account["orders"] and allow_entries and (account["anchor"] is None or account["recenter_pending"]):
        account["anchor"], account["recenter_pending"] = str(mid), False
        account["slots"] = []
        for i in range(settings.grid_count):
            entry = floor(mid * (1 - step * (i + 1)), tick)
            qty = floor(dec(settings.order_notional_usdc) / entry, market["size_step"])
            tp = ((entry * (1 + step)) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
            if qty < dec(market["min_qty"]) or qty * entry < dec(market.get("min_notional", "0")) or tp <= entry:
                raise GridError("QQQ grid order does not meet exchange increments/minimum")
            account["slots"].append({"slot": i + 1, "entry_price": str(entry), "tp_price": str(tp),
                                     "capacity": str(qty), "entered": "0", "qty": "0"})
        if len({s["entry_price"] for s in account["slots"]}) != settings.grid_count:
            raise GridError("QQQ spacing is smaller than the exchange price tick")
    for slot in account["slots"]:
        active = [o for o in account["orders"] if o["slot"] == slot["slot"]]
        buys, sells = [o for o in active if o["side"] == "buy"], [o for o in active if o["side"] == "sell"]
        if not active and not dec(slot["qty"]):
            slot["entered"] = "0"
        if allow_entries and not account["recenter_pending"] and not buys:
            remaining = dec(slot["capacity"]) - dec(slot["entered"])
            if remaining > 0:
                _order(account, slot, "buy", remaining, dec(slot["entry_price"]), market, now, settings)
        uncovered = dec(slot["qty"]) - sum((dec(o["remaining"]) for o in sells), D(0))
        if uncovered > 0:
            _order(account, slot, "sell", uncovered, dec(slot["tp_price"]), market, now, settings)
    if dec(account["qqq"]["qty"]) != sum((dec(s["qty"]) for s in account["slots"]), D(0)):
        raise GridError("QQQ slot and position accounting differ")
    return account, fills


class QQQEngine:
    def __init__(self, config, store):
        self.config, self.store = config, store

    def prepare(self, market, ts):
        return maker_step(self.store.account(), market["lighter"], ts, self.config, market["allow_entries"])

    def calculate(self, frame, plan):
        before = self.store.account()
        if digest(before) != plan["before"]:
            raise GridError("QQQ plan does not match account state")
        account, fills = maker_step(before, frame.market["lighter"], frame.ts, self.config, frame.market["allow_entries"])
        var, settings = frame.market["var"], self.config.settings
        qmark = dec(frame.market["lighter"]["mark"])
        pending, reason = False, "inside_band"
        if var is not None:
            account["last_var_mark"], account["last_var_ts"] = var["mark"], var["ts"]
            target = hedge_target(account, qmark, var["mark"], self.config, var["size_step"])
            change = target - dec(account["us100"]["qty"])
            if str(target) != plan["target"]:
                raise GridError("QQQ hedge target differs from shared observation")
            if change:
                key = format(abs(change).normalize(), "f")
                quote = frame.quotes.get(key)
                reduce_only = var.get("close_only", False) or (quote or {}).get("close_only", False)
                if reduce_only and abs(target) > abs(dec(account["us100"]["qty"])):
                    pending, reason = True, "var_close_only"
                elif quote is None:
                    pending = True
                    reason = "hedge_below_minimum" if abs(change) < dec(var["min_qty"]) else "hedge_quote_unavailable"
                else:
                    price = dec(quote["ask" if change > 0 else "bid"])
                    price *= 1 + (1 if change > 0 else -1) * dec(settings.var_slippage_bps) / 10000
                    fills.append(book_fill(account, "us100", change, price, settings.var_fee_bps,
                                           quote["ts"], "delta_hedge"))
                    account["hedge_adjustments"] += 1
                    reason = "hedged"
        else:
            pending, reason = True, "var_market_unavailable"
        vmark = dec(account["last_var_mark"] or "0")
        net, gross, ratio = exposure(account, qmark, vmark, settings.beta)
        if abs(ratio) > dec(self.config.hedge_tolerance_percent) / 100:
            pending = True
            if reason in {"hedged", "inside_band"}:
                reason = "quantity_rounding_residual"
        legs = {}
        for name, mark in (("qqq", qmark), ("us100", vmark)):
            leg = account[name]
            unrealized = dec(leg["qty"]) * (mark - dec(leg["average_entry"]))
            realized = dec(leg["realized_gross"]) - dec(leg["fees_usdc"])
            legs[name] = {**leg, "mark": str(mark), "notional_usdc": str(dec(leg["qty"]) * mark),
                          "unrealized_pnl_usdc": str(unrealized), "realized_pnl_usdc": str(realized),
                          "total_pnl_usdc": str(realized + unrealized)}
        totals = {k: str(sum((dec(leg[k]) for leg in legs.values()), D(0))) for k in
                  ("total_pnl_usdc", "realized_pnl_usdc", "unrealized_pnl_usdc", "turnover_usdc", "fees_usdc")}
        peak = max(dec(account["peak_pnl"]), dec(totals["total_pnl_usdc"]))
        account["peak_pnl"] = str(peak)
        account["max_drawdown_usdc"] = str(max(dec(account["max_drawdown_usdc"]), peak - dec(totals["total_pnl_usdc"])))
        account["last_ts"] = frame.ts
        snapshot = {"name": self.config.name, "grid_step_percent": self.config.grid_step_percent,
                    "hedge_tolerance_percent": self.config.hedge_tolerance_percent, **totals, **legs,
                    "net_exposure_usdc": str(net), "gross_exposure_usdc": str(gross),
                    "exposure_percent": str(abs(ratio) * 100), "signed_exposure_percent": str(ratio * 100),
                    "hedge_pending": pending, "hedge_status": reason, "hedge_adjustments": account["hedge_adjustments"],
                    "open_slots": sum(dec(s["qty"]) > 0 for s in account["slots"]), "resting_orders": len(account["orders"]),
                    "max_drawdown_usdc": account["max_drawdown_usdc"], "gap_count": account["gap_count"],
                    "var_valued_at": account["last_var_ts"], "anchor": account["anchor"]}
        return account, fills, snapshot

    def apply(self, frame, plan):
        account, fills, snapshot = self.calculate(frame, plan)
        with self.store.transaction():
            for fill in fills:
                self.store.db.execute("INSERT INTO fills VALUES (?,?,?)", (fill["id"], frame.ts, encoded(fill)))
            self.store.set("account", encoded(account))
            self.store.set("last_tick", frame.ts)
            self.store.db.execute("INSERT INTO ticks(ts,snapshot,account) VALUES (?,?,?)", (frame.ts, encoded(snapshot), encoded(account)))
            # Shared summaries keep the full time series; fills keep execution history.
            # Only current and previous account states are needed to read the last
            # published frame if one account commits ahead of the cohort.
            self.store.db.execute("DELETE FROM ticks WHERE ts < (SELECT ts FROM ticks ORDER BY ts DESC LIMIT 1 OFFSET 1)")
        return snapshot
