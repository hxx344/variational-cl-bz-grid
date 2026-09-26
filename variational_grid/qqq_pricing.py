"""Persisted authenticated reference prices for estimated paper fills."""
from dataclasses import asdict, dataclass
import json
import math

from .models import GridError, dec
from .qqq_market import MarketClosed, VAR_SYMBOL, VAR_QUOTE_SOURCE


@dataclass(frozen=True)
class QQQPricing:
    mode: str = "shared_indicative_v1"
    refresh_after_seconds: float = 3
    max_age_seconds: float = 60
    half_spread_percent: str | None = None

    def validate(self):
        if self.mode not in {"shared_indicative_v1", "exact_quantity"}:
            raise GridError("Invalid QQQ pricing mode")
        for value in (self.refresh_after_seconds, self.max_age_seconds):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise GridError("Invalid QQQ cache time limit")
        if not 0 < self.refresh_after_seconds <= self.max_age_seconds <= 600:
            raise GridError("QQQ cache requires 0 < refresh_after_seconds <= max_age_seconds <= 600")
        if self.half_spread_percent is not None and (self.mode != "shared_indicative_v1" or not 0 <= dec(self.half_spread_percent) <= 1):
            raise GridError("Invalid paper half spread percentage")
        return self

    @classmethod
    def load(cls, data):
        try:
            return cls(**data).validate()
        except (TypeError, ValueError):
            raise GridError("Invalid QQQ pricing configuration") from None


def reference_price(quote, policy):
    result = {**quote, "source_bid": quote["bid"], "source_ask": quote["ask"]}
    if policy.half_spread_percent is not None:
        mid = (dec(quote["bid"]) + dec(quote["ask"])) / 2
        half = dec(policy.half_spread_percent) / 100
        result.update(bid=str(mid * (1 - half)), ask=str(mid * (1 + half)))
    return result


def source_valid(quote, market):
    """Validate normalized prices, including historical journal frames."""
    try:
        for row in (quote, market):
            if (row["symbol"], row["instrument_type"], row["multiplier"], row["quantity_unit"]) != (VAR_SYMBOL, "swap", "1", "index_unit"):
                return False
            if type(row["market_open"]) is not bool or type(row["close_only"]) is not bool:
                return False
        for key in ("bid", "ask", "mark", "qty", "min_qty", "max_qty", "size_step"):
            if dec(quote[key]) <= 0:
                return False
        if dec(quote["bid"]) > dec(quote["ask"]) or not dec(quote["min_qty"]) <= dec(quote["qty"]) <= dec(quote["max_qty"]):
            return False
        if dec(quote["qty"]) % dec(quote["size_step"]):
            return False
        for value in (quote["ts"], quote["received_ts"], market["metadata_ts"]):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                return False
        close = market["closes_at"]
        if close is not None and (type(close) not in (int, float) or not math.isfinite(close) or close <= 0):
            return False
        after = market.get("quote_valid_after")
        if after is not None and (type(after) not in (int, float) or not math.isfinite(after) or after <= 0):
            return False
        return quote["ts"] <= quote["received_ts"] + 2
    except (KeyError, TypeError, ValueError, GridError):
        return False


class ReferenceCache:
    def __init__(self, client, path, policy, writer, inherited_path=None):
        self.client, self.path, self.policy, self.writer = client, path, policy, writer
        self.quote, self._saved, self.restore_error = None, None, ""
        source = path if path.exists() else inherited_path
        if source and source.exists():
            try:
                if source.stat().st_size > 64000:
                    raise ValueError()
                raw = source.read_text(encoding="utf-8")
                data = json.loads(raw)
                if data["version"] != 1 or not source_valid(data["quote"], data["market"]):
                    raise ValueError()
                if data["quote"].get("source") != VAR_QUOTE_SOURCE:
                    raise ValueError()
                self.quote = data["quote"]
                client._market = data["market"]
                client._metadata_checked = None
                self._saved = raw if source == path else None
            except (OSError, ValueError, KeyError, TypeError):
                self.restore_error = "旧公共报价或无效缓存已停用，等待 Var token 鉴权报价"

    def usable(self, now):
        if not self.client.session.cache_allowed():
            return None
        quote, market = self.quote, self.client._market
        if not quote or quote.get("source") != VAR_QUOTE_SOURCE or not market or not source_valid(quote, market):
            return None
        if not (-2 <= now - quote["ts"] <= self.policy.max_age_seconds and -2 <= now - market["metadata_ts"] <= 120):
            return None
        if not market["market_open"] or market["closes_at"] is None or now >= market["closes_at"]:
            return None
        if quote["ts"] < (market.get("quote_valid_after") or 0) or quote.get("closes_at") != market["closes_at"]:
            return None
        return {**quote, **{k: market[k] for k in ("market_open", "closes_at", "close_only", "metadata_ts")}}

    def save(self):
        market = self.client._market
        if not self.quote or not market or not source_valid(self.quote, market):
            return
        text = json.dumps({"version": 1, "quote": self.quote, "market": market}, sort_keys=True, separators=(",", ":")) + "\n"
        if text != self._saved:
            self.writer(self.path, text)
            self._saved = text

    def read(self):
        now = self.client.transport.clock()
        current, cache_used, error = self.usable(now), True, self.restore_error
        error_kind = ""
        if current is None or now - current["ts"] > self.policy.refresh_after_seconds:
            try:
                refreshed = self.client.quote("0.01")
                if self.quote is None or refreshed["ts"] >= self.quote["ts"]:
                    self.quote = refreshed
                    cache_used = False
                error, self.restore_error = "", ""
            except GridError as failure:
                error = str(failure)
                error_kind = "market_closed" if isinstance(failure, MarketClosed) else ""
            finally:
                self.save()  # Also persist an observed market closure after a failed quote.
        now = self.client.transport.clock()
        self.save()  # Copy inherited state only after the new cohort exists.
        return self.status(now, cache_used=cache_used, error=error, error_kind=error_kind)

    def status(self, now, *, cache_used, error, error_kind=""):
        # Re-evaluate at the final frame time too: a slow request cannot extend
        # the quote or the independently observed market's lifetime.
        current = self.usable(now)
        status = {**asdict(self.policy), "source_ts": self.quote["ts"] if self.quote else None,
                  "source_qty": self.quote["qty"] if self.quote else None, "cache_used": cache_used,
                  "age_seconds": max(0, now - self.quote["ts"]) if self.quote else None,
                  "available": current is not None, "refresh_error": error, "refresh_error_kind": error_kind,
                  "authentication": "vr-token", "authenticated": self.client.session.confirmed,
                  "authentication_state": self.client.session.state,
                  "authentication_checked_at": now,
                  "authentication_error": self.client.session.error if self.client.session.state in {"rejected", "unavailable"} else "",
                  **self.client.market_observation(now),
                  "source": self.quote.get("source") if self.quote else None}
        if not self.client.session.confirmed and not error:
            status["refresh_error"] = self.client.session.error or error
        if current is None and not status["refresh_error"]:
            status["refresh_error"] = "US100 缓存过期或市场状态不可用，等待有效报价"
        return current, status
