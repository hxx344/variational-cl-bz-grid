from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

D = Decimal
HOUR = 3600
WINDOW = 72


class GridError(Exception):
    """A user-facing error whose message must not contain credentials."""


def dec(value):
    try:
        number = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise GridError("Invalid decimal value") from None
    if not number.is_finite():
        raise GridError("Non-finite decimal value")
    return number


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise GridError("Invalid UTC timestamp") from None


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    center_hours: int = WINDOW
    paper_balance_usdc: str = "1000"
    quantity_barrels: str = "1"
    grid_step_usdc_per_barrel: str = "0.20"
    # None preserves existing absolute-step configurations and ledger identities.
    grid_step_percent: str | None = None
    max_levels: int = 8
    paper_leverage: str = "5"
    max_margin_fraction: str | None = "0.80"  # None disables the paper funding cap.
    max_drawdown_fraction: str = "0.20"
    max_holding_hours: int = 168
    slippage_bps_per_leg: str = "1"
    fee_bps_per_leg: str = "0"
    poll_seconds: int = 10
    max_quote_age_seconds: int = 15
    max_pair_skew_seconds: int = 5
    session_file: str = "data/session.json"
    state_file: str = "data/paper.sqlite3"

    def validate(self):
        if self.mode != "paper":
            raise GridError("Only paper mode is supported; live order execution is absent")
        for name in ("paper_balance_usdc", "quantity_barrels", "grid_step_usdc_per_barrel", "paper_leverage"):
            if dec(getattr(self, name)) <= 0:
                raise GridError(f"{name} must be positive")
        if self.grid_step_percent is not None and not D("0") < dec(self.grid_step_percent) <= D("100"):
            raise GridError("grid_step_percent must be in (0, 100]; 1 means 1%")
        for name in ("max_margin_fraction", "max_drawdown_fraction"):
            if name == "max_margin_fraction" and self.max_margin_fraction is None:
                continue
            if not D("0") < dec(getattr(self, name)) < D("1"):
                raise GridError(f"{name} must be between 0 and 1")
        for name in ("fee_bps_per_leg", "slippage_bps_per_leg"):
            if not D("0") <= dec(getattr(self, name)) < D("1000"):
                raise GridError(f"{name} must be in [0, 1000)")
        for name in ("center_hours", "max_levels", "max_holding_hours", "poll_seconds", "max_quote_age_seconds", "max_pair_skew_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise GridError(f"{name} must be a positive integer")
        if self.max_levels > 100 or self.poll_seconds < 5:
            raise GridError("max_levels must be <= 100 and poll_seconds >= 5")
        if self.center_hours not in (72, 168):
            raise GridError("center_hours must be 72 (3 days) or 168 (legacy 7 days)")
        if not isinstance(self.session_file, str) or not isinstance(self.state_file, str) or not self.session_file.strip() or not self.state_file.strip():
            raise GridError("File paths must be non-empty strings")
        return self

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            data.setdefault("center_hours", 168)  # Unversioned files describe the old strategy.
            return cls(**data).validate()
        except (OSError, ValueError, TypeError):
            raise GridError("Cannot read configuration, or configuration contains unknown fields") from None

    def strategy_identity(self):
        # Changing economics while a ledger is open must never silently reinterpret it.
        data = asdict(self)
        if self.center_hours == 168:
            data.pop("center_hours")  # Preserve the exact legacy ledger identity.
        if self.grid_step_percent is None:
            data.pop("grid_step_percent")
        else:
            data.pop("grid_step_usdc_per_barrel")
        for name in ("poll_seconds", "max_quote_age_seconds", "max_pair_skew_seconds", "session_file", "state_file"):
            data.pop(name)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def grid_step(self, center):
        if self.grid_step_percent is None:
            return dec(self.grid_step_usdc_per_barrel)
        return abs(dec(center)) * dec(self.grid_step_percent) / 100

    def grid_geometry(self, center):
        center = dec(center)
        distance = self.grid_step(center) * self.max_levels
        percent = dec(self.grid_step_percent) * self.max_levels if self.grid_step_percent is not None else None
        return {"grid_range_percent": str(percent) if percent is not None else None,
                "grid_span_percent": str(2 * percent) if percent is not None else None,
                "grid_lower": str(center - distance), "grid_upper": str(center + distance)}


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    mark: Decimal
    qty: Decimal
    ts: float

    @classmethod
    def parse(cls, symbol, qty, data):
        try:
            expected = {"underlying": symbol, "instrument_type": "perpetual_rwa_future", "settlement_asset": "USDC", "kind": "commodity"}
            if data["instrument"] != expected or dec(data["qty"]) != qty:
                raise GridError("Quote instrument or quantity does not match request")
            quote = cls(symbol, dec(data["bid"]), dec(data["ask"]), dec(data["mark_price"]), qty, timestamp(data["timestamp"]))
            if quote.bid <= 0 or quote.ask < quote.bid or quote.mark <= 0:
                raise GridError("Invalid bid, ask, or mark price")
            for side in ("bid", "ask"):
                limits = data["qty_limits"][side]
                tick, minimum, maximum = (dec(limits[k]) for k in ("min_qty_tick", "min_qty", "max_qty"))
                if tick <= 0 or qty % tick or not minimum <= qty <= maximum:
                    raise GridError("Configured quantity is outside the venue quote limits")
            return quote
        except (KeyError, TypeError):
            raise GridError("Quote response schema changed") from None


def validate_pair(cl, bz, now, config):
    if cl.symbol != "CL" or bz.symbol != "BZ" or cl.qty != bz.qty or cl.qty != dec(config.quantity_barrels):
        raise GridError("CL/BZ quantities or symbols do not match")
    for quote in (cl, bz):
        if quote.ts > now + 2 or now - quote.ts > config.max_quote_age_seconds:
            raise GridError("Quote is stale or from the future")
    if abs(cl.ts - bz.ts) > config.max_pair_skew_seconds:
        raise GridError("CL/BZ quote timestamps are too far apart")


def rolling_center(cl_rows, bz_rows, hour_end, hours=WINDOW):
    """Exactly the configured number of aligned, CLOSED hourly candles. No fills."""
    if type(hours) is not int or hours not in (72, 168):
        raise GridError("Invalid center window")
    if hour_end % HOUR:
        raise GridError("History endpoint must be an exact UTC hour")
    start = hour_end - hours * HOUR
    expected = set(range(start, hour_end, HOUR))
    def parse(rows):
        values = {}
        try:
            for row in rows:
                raw = dec(row["unix_time_ms"])
                if raw % (HOUR * 1000):
                    raise GridError("Candle timestamp is not hourly aligned")
                ts = int(raw / 1000)
                if ts not in expected:
                    continue
                if ts in values:
                    raise GridError("Duplicate candle timestamp")
                price = dec(row["close"])
                if price <= 0:
                    raise GridError("Invalid candle close")
                values[ts] = price
        except (KeyError, TypeError):
            raise GridError("Candle response schema changed") from None
        if set(values) != expected:
            raise GridError(f"Incomplete {hours // 24}-day history: {len(values)}/{hours} aligned hours")
        return values
    cl, bz = parse(cl_rows), parse(bz_rows)
    return sum((bz[t] - cl[t] for t in sorted(expected)), D(0)) / hours
