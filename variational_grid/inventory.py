"""Inventory experiments: shared signal books and one net paper account per band.

Signal lots are intentions, not executions. Only changes of the aggregate account
position generate fills, costs or volume. No network or live execution exists here.
"""
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR
import copy
import hashlib
import json
from pathlib import Path
import sqlite3

from .models import D, GridError, HOUR, dec, utc
from .store import Store

SYMBOLS = ("CL", "BZ")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def quote_key(symbol, quantity):
    return symbol + ":" + format(abs(dec(quantity)).normalize(), "f")


def exposure(positions):
    cl, bz = (dec(positions[s]) for s in SYMBOLS)
    gross = abs(cl) + abs(bz)
    return cl + bz, (bz - cl) / 2, abs(cl + bz) / gross if gross else D(0)


@dataclass(frozen=True)
class InventorySettings:
    scalp_step_percent: str = "0.15"
    scalp_take_profit_percent: str = "0.10"
    scalp_cooldown_seconds: int = 60
    ordinary_step_percent: str = "0.5"
    spread_step_percent: str = "1"
    spread_direction: str = "long"
    execution_step_barrels: str = "0.01"
    reset_fraction: str = "0.5"
    risk_reference_percent: str = "20"
    enable_scalp: bool = True
    enable_ordinary: bool = True
    enable_spread: bool = True

    def validate(self):
        for name in ("scalp_step_percent", "scalp_take_profit_percent", "ordinary_step_percent", "spread_step_percent"):
            if not 0 < dec(getattr(self, name)) <= 100:
                raise GridError(name + " must be a percentage in (0, 100]")
        if dec(self.execution_step_barrels) <= 0 or not 0 <= dec(self.reset_fraction) < 1:
            raise GridError("Invalid inventory execution step or reset fraction")
        if not 0 <= dec(self.risk_reference_percent) < 100:
            raise GridError("Invalid common exposure comparison threshold")
        if type(self.scalp_cooldown_seconds) is not int or self.scalp_cooldown_seconds < 0:
            raise GridError("Invalid scalp cooldown")
        if self.spread_direction not in {"long", "both"}:
            raise GridError("spread_direction must be long or both")
        for name in ("enable_scalp", "enable_ordinary", "enable_spread"):
            if type(getattr(self, name)) is not bool:
                raise GridError("Strategy switches must be boolean")
        if not any((self.enable_scalp, self.enable_ordinary, self.enable_spread)):
            raise GridError("Enable at least one inventory signal strategy")
        return self


@dataclass(frozen=True)
class InventoryConfig:
    base: object
    settings: InventorySettings
    name: str
    tolerance_percent: str | None
    state_file: str

    def __getattr__(self, name):
        return getattr(self.base, name)

    def strategy_identity(self):
        return encoded({"kind": "inventory", "version": 1, "base": json.loads(self.base.strategy_identity()),
                        "strategy": asdict(self.settings), "tolerance_percent": self.tolerance_percent})


def empty_signals():
    return {"next_id": 1, "lots": [], "blocked": [], "last_open": {}}


def signal_step(previous, marks, centers, now, config, allow_open=True):
    """Same mark-based signal decisions for every account; never assume a fill.

    Targets are mark-price distances, not promised net take profits. All signal
    books are evaluated before account netting and actual quote-based accounting.
    """
    state = copy.deepcopy(previous)
    settings = config.settings
    qty = dec(config.quantity_barrels)
    marks = {s: dec(marks[s]) for s in SYMBOLS}
    spread = marks["BZ"] - marks["CL"]
    closed_books = set()
    retained = []
    events = []
    for lot in state["lots"]:
        value = spread if lot["symbol"] == "SPREAD" else marks[lot["symbol"]]
        expired = now - lot["opened"] >= config.max_holding_hours * HOUR
        if lot["direction"] * (value - dec(lot["entry"])) >= dec(lot["distance"]) or expired:
            closed_books.add(lot["book"])
            events.append({"action": "signal_close", "id": lot["id"], "book": lot["book"],
                           "reason": "max_holding" if expired else "price_target"})
            if lot["level"]:
                state["blocked"].append([lot["book"], lot["direction"], lot["level"]])
        else:
            retained.append(lot)
    state["lots"] = retained

    def opened(book, symbol, direction, level, entry, distance):
        state["lots"].append({"id": state["next_id"], "book": book, "symbol": symbol,
                              "direction": direction, "level": level, "qty": str(qty),
                              "entry": str(entry), "distance": str(distance), "opened": now})
        state["next_id"] += 1
        state["last_open"][book] = now
        events.append({"action": "signal_open", "book": book})

    if settings.enable_scalp and allow_open:
        for symbol, direction in (("CL", 1), ("BZ", -1)):
            book = "scalp_" + symbol
            active = [lot for lot in state["lots"] if lot["book"] == book]
            if book in closed_books or now - state["last_open"].get(book, -1e30) < settings.scalp_cooldown_seconds:
                continue
            step = dec(settings.scalp_step_percent) / 100
            eligible = not active
            if active:
                edge = (min if direction == 1 else max)(dec(lot["entry"]) for lot in active)
                eligible = direction * (marks[symbol] - edge) <= -edge * step
            if eligible:
                opened(book, symbol, direction, 0, marks[symbol], marks[symbol] * dec(settings.scalp_take_profit_percent) / 100)

    books = []
    if settings.enable_ordinary:
        books.extend(("ordinary_" + s, s, marks[s], dec(centers[s]), dec(settings.ordinary_step_percent)) for s in SYMBOLS)
    if settings.enable_spread:
        books.append(("spread", "SPREAD", spread, dec(centers["spread"]), dec(settings.spread_step_percent)))
    for book, symbol, price, center, percent in books:
        distance = abs(center) * percent / 100
        if distance == 0:
            continue
        def reached(direction, level):
            return direction * (center - price) >= level * distance
        state["blocked"] = [x for x in state["blocked"] if x[0] != book or reached(x[1], x[2])]
        if not allow_open or book in closed_books:
            continue
        direction = 1 if price < center else -1
        if book == "spread" and settings.spread_direction == "long" and direction != 1:
            continue
        used = {lot["level"] for lot in state["lots"] if lot["book"] == book and lot["direction"] == direction}
        used.update(x[2] for x in state["blocked"] if x[:2] == [book, direction])
        level = 1
        while level in used:
            level += 1
        if reached(direction, level):
            opened(book, symbol, direction, level, price, distance)
    raw = {s: D(0) for s in SYMBOLS}
    components = {}
    for lot in state["lots"]:
        component = components.setdefault(lot["book"], {s: D(0) for s in SYMBOLS})
        amount = dec(lot["qty"]) * lot["direction"]
        legs = {"CL": -amount, "BZ": amount} if lot["symbol"] == "SPREAD" else {lot["symbol"]: amount}
        for symbol, quantity in legs.items():
            raw[symbol] += quantity
            component[symbol] += quantity
    return state, {s: str(v) for s, v in raw.items()}, {k: {s: str(v) for s, v in p.items()} for k, p in components.items()}, events


def project_inventory(raw, overlay, tolerance_percent, step, reset_fraction="0.5"):
    """Project along (1,1), preserving spread sensitivity including rounding.

    Only executable common-step candidates are considered. If exact neutrality
    is impossible, the smallest residual is reported instead of inventing fills.
    """
    raw = {s: dec(raw[s]) for s in SYMBOLS}
    step, old = dec(step), dec(overlay)
    if tolerance_percent is None:
        return raw, D(0), "uncontrolled"
    current = {s: raw[s] + old for s in SYMBOLS}
    direction, spread, ratio = exposure(current)
    threshold = dec(tolerance_percent) / 100
    if all(v == 0 for v in raw.values()):
        return raw, D(0), "inside"
    if ratio <= threshold:
        return current, old, "inside"
    target_d = (1 if direction > 0 else -1) * threshold * 2 * abs(spread) * dec(reset_fraction)
    ideal = old + (target_d - direction) / 2
    candidates = {ideal / step, -(raw["CL"] + raw["BZ"]) / (2 * step)}
    candidates = {v.to_integral_value(rounding=rounding) * step for v in candidates for rounding in (ROUND_FLOOR, ROUND_CEILING)}
    def score(k):
        d, _, r = exposure({s: raw[s] + k for s in SYMBOLS})
        return (r > threshold, abs(d - target_d), abs(k - old))
    chosen = min(candidates, key=lambda k: (*score(k), k))
    target = {s: raw[s] + chosen for s in SYMBOLS}
    status = "adjusted" if exposure(target)[2] <= threshold else "quantity_limited"
    return target, chosen, status


def initial_account(config):
    return {"positions": {s: {"qty": "0", "average_price": "0"} for s in SYMBOLS}, "overlay": "0",
            "realized_gross": "0", "fees": "0", "execution_cost": "0", "peak": config.paper_balance_usdc,
            "max_drawdown": "0", "volume": "0", "turnover": "0", "fill_count": 0,
            "hedge_adjustments": 0, "hedge_turnover": "0", "hedge_cost": "0", "direction_pnl": "0", "spread_pnl": "0",
            "observed_seconds": 0, "exposure_seconds": 0, "direction_barrel_seconds": "0", "breach_seconds": 0,
            "max_ratio": "0", "max_abs_direction": "0", "last_marks": None, "last_ts": None, "halted": ""}


class InventoryStore:
    get = Store.get
    set = Store.set
    transaction = Store.transaction
    snapshot = Store.snapshot
    close = Store.close

    def __init__(self, path, config):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ticks(id INTEGER PRIMARY KEY,ts REAL UNIQUE NOT NULL,snapshot TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,ts REAL NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,
          qty TEXT NOT NULL,price TEXT NOT NULL,fee TEXT NOT NULL,realized_pnl_usdc TEXT NOT NULL,
          execution_cost_usdc TEXT NOT NULL,hedge_qty TEXT NOT NULL,reason TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS fills_time ON fills(ts);
        """)
        identity = self.get("config")
        if identity is not None and identity != config.strategy_identity():
            self.close()
            raise GridError("Inventory settings changed; use a new experiment output directory")
        if identity is None:
            self.reset(config)

    def reset(self, config):
        with self.transaction():
            for table in ("meta", "ticks", "fills"):
                self.db.execute("DELETE FROM " + table)
            self.set("config", config.strategy_identity())
            self.set("account", encoded(initial_account(config)))

    def account(self):
        return json.loads(self.get("account"))


class InventoryEngine:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.fee = dec(config.fee_bps_per_leg) / 10000
        self.slip = dec(config.slippage_bps_per_leg) / 10000

    def preview(self, raw, step, allow_open=True):
        account = self.store.account()
        target, overlay, status = project_inventory(raw, account["overlay"], self.config.tolerance_percent,
                                                    step, self.config.settings.reset_fraction)
        if account["halted"]:
            target, overlay, status = {s: D(0) for s in SYMBOLS}, D(0), "halted"
        elif not allow_open:
            for symbol in SYMBOLS:
                current = dec(account["positions"][symbol]["qty"])
                target[symbol] = (max(D(0), min(current, target[symbol])) if current >= 0
                                  else min(D(0), max(current, target[symbol])))
            status = "close_only"
        return {"before": digest(account), "target": {s: str(v) for s, v in target.items()},
                "overlay": str(overlay), "control_status": status, "halted": account["halted"]}

    def price(self, quote, side):
        return dec(quote["ask"]) * (1 + self.slip) if side > 0 else dec(quote["bid"]) * (1 - self.slip)

    def estimate(self, account, frame):
        mark_unrealized, exit_unrealized = D(0), D(0)
        notional = D(0)
        positions = []
        for symbol in SYMBOLS:
            position = account["positions"][symbol]
            quantity, average = dec(position["qty"]), dec(position["average_price"])
            mark = dec(frame.marks[symbol])
            notional += abs(quantity) * mark
            mark_unrealized += quantity * (mark - average)
            unrealized = D(0)
            if quantity:
                quote = frame.quotes[quote_key(symbol, quantity)]
                exit_price = self.price(quote, -quantity)
                unrealized = quantity * (exit_price - average) - abs(quantity) * exit_price * self.fee
            exit_unrealized += unrealized
            positions.append({"symbol": symbol, "qty": str(quantity), "average_price": str(average),
                              "mark_price": str(mark), "unrealized_pnl_usdc": str(unrealized), "valued_at": frame.ts})
        realized = dec(account["realized_gross"]) - dec(account["fees"])
        equity = dec(self.config.paper_balance_usdc) + realized + exit_unrealized
        return equity, realized, exit_unrealized, mark_unrealized - exit_unrealized, notional, positions

    def calculate(self, frame, plan):
        """Pure account transition. Used before journaling and identically on replay."""
        account = self.store.account()
        if digest(account) != plan["before"]:
            raise GridError("Inventory frame does not match the saved account")
        account["peak"] = str(max(dec(account["peak"]), dec(plan.get("observed_peak", account["peak"]))))
        previous_positions = {s: dec(account["positions"][s]["qty"]) for s in SYMBOLS}
        old_d, old_h, old_ratio = exposure(previous_positions)
        if account["last_marks"] is not None:
            changes = {s: dec(frame.marks[s]) - dec(account["last_marks"][s]) for s in SYMBOLS}
            account["direction_pnl"] = str(dec(account["direction_pnl"]) + old_d * (changes["CL"] + changes["BZ"]) / 2)
            account["spread_pnl"] = str(dec(account["spread_pnl"]) + old_h * (changes["BZ"] - changes["CL"]))
            elapsed = frame.ts - account["last_ts"]
            if 0 < elapsed <= max(60, self.config.poll_seconds * 3):
                account["observed_seconds"] += elapsed
                if old_ratio > dec(self.config.settings.risk_reference_percent) / 100:
                    account["exposure_seconds"] += elapsed
                if self.config.tolerance_percent is not None and old_ratio > dec(self.config.tolerance_percent) / 100:
                    account["breach_seconds"] += elapsed
                account["direction_barrel_seconds"] = str(dec(account["direction_barrel_seconds"]) + abs(old_d) * dec(elapsed))
        hedge_delta = dec(plan["overlay"]) - dec(account["overlay"])
        fills = []
        for symbol in SYMBOLS:
            position = account["positions"][symbol]
            current, average = dec(position["qty"]), dec(position["average_price"])
            target = dec(plan["target"][symbol])
            change = target - current
            if not change:
                continue
            quote = frame.quotes[quote_key(symbol, change)]
            price = self.price(quote, change)
            fee = abs(change) * price * self.fee
            realized = D(0)
            if current == 0 or current * change > 0:
                new_average = (abs(current) * average + abs(change) * price) / abs(target)
            else:
                closed = min(abs(current), abs(change))
                realized = closed * (price - average) * (1 if current > 0 else -1)
                new_average = D(0) if target == 0 else average if current * target > 0 else price
            cost = change * (price - dec(frame.marks[symbol])) + fee
            # Attribute only the surviving external portion, never internally offset orders.
            hedge_qty = min(abs(change), abs(hedge_delta)) if change * hedge_delta > 0 and not plan["halted"] else D(0)
            account["hedge_turnover"] = str(dec(account["hedge_turnover"]) + hedge_qty * price)
            account["hedge_cost"] = str(dec(account["hedge_cost"]) + cost * hedge_qty / abs(change))
            account["positions"][symbol] = {"qty": str(target), "average_price": str(new_average)}
            for key, amount in (("realized_gross", realized), ("fees", fee), ("execution_cost", cost),
                                ("volume", abs(change)), ("turnover", abs(change) * price)):
                account[key] = str(dec(account[key]) + amount)
            account["fill_count"] += 1
            fills.append({"ts": frame.ts, "symbol": symbol, "side": "buy" if change > 0 else "sell", "qty": str(abs(change)),
                          "price": str(price), "fee": str(fee), "realized_pnl_usdc": str(realized - fee),
                          "execution_cost_usdc": str(cost), "hedge_qty": str(hedge_qty),
                          "reason": plan["halted"] or ("signal_and_inventory" if hedge_qty else "signal")})
        if hedge_delta and not plan["halted"]:
            account["hedge_adjustments"] += 1
        account.update(overlay=plan["overlay"], halted=plan["halted"], last_marks=frame.marks, last_ts=frame.ts)
        equity, realized, unrealized, reserve, notional, positions = self.estimate(account, frame)
        peak = max(dec(account["peak"]), equity)
        drawdown = max(D(0), (peak - equity) / peak)
        account["peak"] = str(peak)
        account["max_drawdown"] = str(max(dec(account["max_drawdown"]), drawdown))
        direction, spread, ratio = exposure(plan["target"])
        account["max_ratio"] = str(max(dec(account["max_ratio"]), ratio))
        account["max_abs_direction"] = str(max(dec(account["max_abs_direction"]), abs(direction)))
        raw_d = exposure(frame.raw)[0]
        a = dec(frame.components.get("scalp_CL", {}).get("CL", "0"))
        b = -dec(frame.components.get("scalp_BZ", {}).get("BZ", "0"))
        status = plan["control_status"]
        snapshot = {"name": self.config.name, "ts": frame.ts, "time_utc": utc(frame.ts),
                    "tolerance_percent": self.config.tolerance_percent, "control_status": status,
                    "control_reason": "Venue quantity step prevents exact target" if status == "quantity_limited" else None,
                    "cl_barrels": plan["target"]["CL"], "bz_barrels": plan["target"]["BZ"],
                    "directional_barrels": str(direction), "spread_barrels": str(spread), "inventory_ratio": str(ratio),
                    "max_inventory_ratio": account["max_ratio"], "max_abs_directional_barrels": account["max_abs_direction"],
                    "scalp_cl_long_barrels": str(a), "scalp_bz_short_barrels": str(b), "scalp_difference_barrels": str(a - b),
                    "raw_directional_barrels": str(raw_d), "overlay_barrels_per_leg": plan["overlay"],
                    "equity_usdc": str(equity), "total_pnl_usdc": str(equity - dec(self.config.paper_balance_usdc)),
                    "realized_pnl_usdc": str(realized), "unrealized_pnl_usdc": str(unrealized),
                    "drawdown_fraction": str(drawdown), "max_drawdown_fraction": account["max_drawdown"],
                    "position_notional_usdc": str(notional), "margin_usdc": str(notional / dec(self.config.paper_leverage)),
                    "volume_barrels": account["volume"], "turnover_usdc": account["turnover"], "fill_count": account["fill_count"],
                    "fees_usdc": account["fees"], "execution_cost_usdc": account["execution_cost"], "exit_cost_reserve_usdc": str(reserve),
                    "direction_pnl_usdc": account["direction_pnl"], "spread_pnl_usdc": account["spread_pnl"],
                    "hedge_adjustments": account["hedge_adjustments"], "hedge_turnover_usdc": account["hedge_turnover"],
                    "hedge_cost_usdc": account["hedge_cost"], "observed_seconds": account["observed_seconds"],
                    "exposure_seconds": account["exposure_seconds"], "breach_seconds": account["breach_seconds"],
                    "mean_abs_directional_barrels": str(dec(account["direction_barrel_seconds"]) / dec(account["observed_seconds"])) if account["observed_seconds"] else "0",
                    "initial_balance_usdc": self.config.paper_balance_usdc, "paper_leverage": self.config.paper_leverage,
                    "halted": account["halted"], "pnl_basis": "before_funding", "positions": positions,
                    "components": frame.components, "signal_lots": len(frame.signals["lots"])}
        return account, fills, snapshot

    def apply(self, frame, plan):
        account, fills, snapshot = self.calculate(frame, plan)
        with self.store.transaction():
            for fill in fills:
                columns = tuple(fill)
                self.store.db.execute("INSERT INTO fills(" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", tuple(fill.values()))
            self.store.set("account", encoded(account))
            self.store.set("last_tick", frame.ts)
            self.store.db.execute("INSERT INTO ticks(ts,snapshot) VALUES (?,?)", (frame.ts, encoded(snapshot)))
        return snapshot
