"""Read-only QQQ book/trades and US100 swap indicative market data.

No signer, order, quote-acceptance or account endpoint exists here. Lighter's
REST book has no exchange timestamp/sequence; HTTP Date is labelled as such,
and a recent-trades overlap plus bounded poll interval guards simulated fills.
The first batch and every lost interval only establish a new queue anchor.
"""
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .client import NoRedirect, ORIGIN, USER_AGENT
from .models import GridError, timestamp


LIGHTER_ORIGIN = "https://mainnet.zklighter.elliot.ai"
LIGHTER_MARKET_ID = 129
VAR_SYMBOL = "US100S"
VAR_INSTRUMENT = {"underlying": VAR_SYMBOL, "instrument_type": "swap",
                  "settlement_asset": "USDC", "kind": "index", "funding_interval_s": 0}
SOURCE_URLS = {
    "lighter_market": LIGHTER_ORIGIN + "/api/v1/orderBookDetails?market_id=129",
    "lighter_book": LIGHTER_ORIGIN + "/api/v1/orderBookOrders?market_id=129&limit=250",
    "lighter_trades": LIGHTER_ORIGIN + "/api/v1/recentTrades?market_id=129&limit=100",
    "lighter_schema": "https://github.com/elliottech/lighter-python/blob/main/lighter/models/trade.py",
    "var_market": ORIGIN + "/api/metadata/supported_assets?cex_asset=US100S",
    "var_quotes": ORIGIN + "/api/quotes/simple",
    "var_ui": ORIGIN + "/swap/US100S",
}


def _number(value, *, zero=False):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (not zero and result == 0) or result > Decimal("1e30"):
            raise ValueError()
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise GridError("Invalid market numeric field") from None


def _text(value):
    return format(value, "f")


def _integer(value, minimum=0, maximum=10**20):
    if type(value) is not int or not minimum <= value <= maximum:
        raise GridError("Invalid market integer field")
    return value


def _fresh(source_ts, now, maximum_age):
    # Exchange clocks may differ slightly; larger future values are rejected.
    return -2 <= now - source_ts <= maximum_age


class _Transport:
    def __init__(self, origin, allowed, opener=None, clock=None):
        self.origin = origin
        self.allowed = allowed
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.clock = clock or time.time

    def request(self, method, path, *, query=None, body=None):
        if (method, path) not in self.allowed:
            raise GridError("Endpoint is not permitted by the QQQ paper market client")
        url = self.origin + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
                   "Cache-Control": "no-cache"}
        if self.origin == ORIGIN:
            headers["Referer"] = ORIGIN + "/"
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        requested = self.clock()
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise GridError("Market response exceeds size limit")
                data = json.loads(raw, parse_float=Decimal)
                if not isinstance(data, dict):
                    raise GridError("Unexpected market response schema")
                received = self.clock()
                date = response.headers.get("Date")
                date_ts = parsedate_to_datetime(date).timestamp() if date else None
                source_ts = None
                if date_ts is not None and date_ts <= received + 2 and received >= requested:
                    # RFC 9111 section 4.2.3: Date lag and Age are independent
                    # estimates of the SAME age; adding them double-counts it.
                    # This also handles proxies that refresh Date but retain Age.
                    age = float(_number(response.headers.get("Age", "0"), zero=True))
                    apparent_age = max(0, received - date_ts)
                    corrected_age = age + received - requested
                    source_ts = received - max(apparent_age, corrected_age)
                return data, received, source_ts
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise GridError(f"Market API HTTP {code}; no simulated execution accepted") from None
        except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
            raise GridError("Market transport or JSON error; no simulated execution accepted") from None


class LighterClient:
    """Public QQQ feed; every returned trade side is the aggressor's side."""

    def __init__(self, *, opener=None, clock=None, max_age_seconds=10, max_gap_seconds=10, max_metadata_age_seconds=30):
        self.transport = _Transport(LIGHTER_ORIGIN, {
            ("GET", "/api/v1/orderBookDetails"), ("GET", "/api/v1/orderBookOrders"),
            ("GET", "/api/v1/recentTrades"),
        }, opener, clock)
        self.max_age_seconds = max_age_seconds
        self.max_gap_seconds = max_gap_seconds
        self.max_metadata_age_seconds = max_metadata_age_seconds
        self._cursor = None
        self._cursor_ts = None
        self._last_poll = None
        self._needs_anchor = True

    @staticmethod
    def _metadata(data):
        try:
            rows = data["order_book_details"]
            rows = [row for row in rows if row["market_id"] == LIGHTER_MARKET_ID]
            if data["code"] != 200 or len(rows) != 1:
                raise GridError("QQQ market metadata unavailable")
            row = rows[0]
            if row["symbol"] != "QQQ" or row["market_type"] != "perp" or _number(row["multiplier"]) != 1:
                raise GridError("QQQ market identity or contract multiplier changed")
            price_decimals = _integer(row["supported_price_decimals"], 0, 12)
            size_decimals = _integer(row["supported_size_decimals"], 0, 12)
            return {"symbol": "QQQ", "market_id": LIGHTER_MARKET_ID, "multiplier": "1",
                    "price_tick": _text(Decimal(10) ** -price_decimals),
                    "size_step": _text(Decimal(10) ** -size_decimals),
                    "min_qty": _text(_number(row["min_base_amount"])),
                    "min_notional": _text(_number(row["min_quote_amount"])),
                    "mark": _text(_number(row["mark_price"])),
                    "market_open": row["status"] == "active" and row.get("is_frozen") is False,
                    "close_only": row.get("market_config", {}).get("force_reduce_only", False) is not False,
                    "funding_status": "missing", "funding_rate": None}
        except (KeyError, TypeError, AttributeError):
            raise GridError("QQQ metadata schema changed") from None

    @staticmethod
    def _book(data):
        try:
            if data["code"] != 200:
                raise GridError("QQQ book unavailable")
            sides = []
            for name in ("bids", "asks"):
                rows = data[name]
                if not isinstance(rows, list) or not 0 < len(rows) <= 250:
                    raise GridError("QQQ depth is empty or invalid")
                totals = {}
                for row in rows:
                    price = _number(row["price"])
                    qty = _number(row["remaining_base_amount"], zero=True)
                    if qty:
                        totals[price] = totals.get(price, Decimal(0)) + qty
                levels = [[_text(p), _text(totals[p])] for p in sorted(totals, reverse=name == "bids")]
                if not levels:
                    raise GridError("QQQ depth is empty")
                sides.append(levels)
            bids, asks = sides
            if Decimal(bids[0][0]) >= Decimal(asks[0][0]):
                raise GridError("QQQ book is crossed")
            return bids, asks
        except (KeyError, TypeError, AttributeError):
            raise GridError("QQQ book schema changed") from None

    @staticmethod
    def _trades(data):
        try:
            if data["code"] != 200 or not isinstance(data["trades"], list) or len(data["trades"]) > 100:
                raise GridError("QQQ trade window unavailable")
            parsed, seen = [], set()
            for row in data["trades"]:
                identity = _integer(row["trade_id"], 1)
                if row.get("trade_id_str", str(identity)) != str(identity) or identity in seen:
                    raise GridError("QQQ trade identity is inconsistent")
                seen.add(identity)
                if row["market_id"] != LIGHTER_MARKET_ID or type(row["is_maker_ask"]) is not bool:
                    raise GridError("QQQ trade market or aggressor is invalid")
                kind = row["type"]
                if kind not in {"trade", "liquidation", "deleverage", "market-settlement"}:
                    raise GridError("QQQ trade type changed")
                source_ts = _integer(row["timestamp"], 1_420_070_400_000, 4_102_444_800_000) / 1000
                parsed.append({"id": str(identity), "ts": source_ts,
                               "price": _text(_number(row["price"])), "qty": _text(_number(row["size"])),
                               "side": "buy" if row["is_maker_ask"] else "sell", "type": kind})
            return sorted(parsed, key=lambda row: int(row["id"]))
        except (KeyError, TypeError, AttributeError):
            raise GridError("QQQ trade schema changed") from None

    def snapshot(self):
        try:
            return self._snapshot()
        except GridError:
            # Never replay a response interval spanning a failed read.
            self._needs_anchor = True
            raise

    def _snapshot(self):
        details, _, details_ts = self.transport.request("GET", "/api/v1/orderBookDetails", query={"market_id": LIGHTER_MARKET_ID})
        result = self._metadata(details)
        book, _, book_ts = self.transport.request("GET", "/api/v1/orderBookOrders", query={"market_id": LIGHTER_MARKET_ID, "limit": 250})
        bids, asks = self._book(book)
        raw_trades, now, trades_ts = self.transport.request("GET", "/api/v1/recentTrades", query={"market_id": LIGHTER_MARKET_ID, "limit": 100})
        trades = self._trades(raw_trades)
        source_times = {"metadata": details_ts, "book": book_ts, "trades": trades_ts}
        stale_sources = [name for name, value in source_times.items()
                         if value is None or not _fresh(value, now, self.max_metadata_age_seconds if name == "metadata" else self.max_age_seconds)]
        fresh = not stale_sources
        ready = fresh and result["market_open"]
        reason = "" if ready else ("stale_http_source" if not fresh else "market_closed")
        gap = self._needs_anchor
        if gap and ready:
            reason = "bootstrap_or_recovery"
        ids = {trade["id"] for trade in trades}
        if self._last_poll is not None and not 0 <= now - self._last_poll <= self.max_gap_seconds:
            gap, reason = True, "poll_gap"
        if self._cursor is not None and self._cursor not in ids:
            gap, reason = True, "trade_window_gap"
        regressed = self._cursor is not None and bool(trades) and int(trades[-1]["id"]) < int(self._cursor)
        if regressed:
            gap, reason = True, "trade_window_regressed"
        new = [trade for trade in trades if self._cursor is None or int(trade["id"]) > int(self._cursor)]
        previous_ts = self._cursor_ts
        for trade in new:
            if trade["ts"] > now + 2 or (previous_ts is not None and trade["ts"] < previous_ts):
                gap, reason = True, "trade_time_gap"
            previous_ts = trade["ts"]
        # Delayed trades are never offered as current maker fills.
        if self._cursor is not None and any(not _fresh(trade["ts"], now, self.max_age_seconds) for trade in new):
            gap, reason = True, "stale_trade_batch"
        if not ready:
            gap = True
        accepted = [trade for trade in new if trade["type"] == "trade"] if ready and not gap else []
        if trades and not regressed:
            newest = trades[-1]
            self._cursor, self._cursor_ts = newest["id"], newest["ts"]
        self._last_poll = now
        self._needs_anchor = not ready or regressed or self._cursor is None
        return {**result, "ts": now, "source_ts": book_ts,
                "source_time_kind": "http_cache_age_book_exchange_time_unavailable",
                "source": "lighter_public_rest", "bid": bids[0][0], "ask": asks[0][0],
                # Cached metadata's venue mark must not silently value a current
                # book snapshot. Keep it separately with its own source time.
                "mark": _text((Decimal(bids[0][0]) + Decimal(asks[0][0])) / 2),
                "mark_source": "book_mid", "venue_mark": result["mark"], "venue_mark_ts": details_ts,
                "metadata_ts": details_ts, "trade_source_ts": trades_ts, "stale_sources": stale_sources,
                "bids": bids, "asks": asks, "trades": accepted,
                "depth_truncated": len(book["bids"]) == 250 or len(book["asks"]) == 250,
                "bid_depth_floor": bids[-1][0], "ask_depth_ceiling": asks[-1][0],
                "latest_trade_ts": trades[-1]["ts"] if trades else None,
                "trade_cursor": self._cursor, "gap": gap, "ready": ready, "reason": reason}


class VarSwapClient:
    """US100S display quotes in index units (not CME $20 contracts).

The venue frontend computes dollar notional as quantity times quoted price.
Public /quotes/simple supplies quantity-specific indicative bid/ask prices.
    No session or account credentials are read.
"""

    def __init__(self, *, opener=None, clock=None, max_age_seconds=10):
        self.transport = _Transport(ORIGIN, {
            ("GET", "/api/metadata/supported_assets"), ("POST", "/api/quotes/simple"),
        }, opener, clock)
        self.max_age_seconds = max_age_seconds
        self._market = None
        self._metadata_checked = None

    def _metadata(self):
        now = self.transport.clock()
        if self._market is not None and self._metadata_checked is not None and 0 <= now - self._metadata_checked < 5:
            return self._market
        data, received, source_ts = self.transport.request("GET", "/api/metadata/supported_assets", query={"cex_asset": VAR_SYMBOL})
        try:
            rows = [row for row in data[VAR_SYMBOL] if row.get("asset") == VAR_SYMBOL and row.get("has_perp") is True]
            if len(rows) != 1:
                raise GridError("US100 swap metadata unavailable")
            row = rows[0]
            if row["instrument_type"] != "swap" or row["asset_class"] != "index" or row["funding_interval_s"] != 0:
                raise GridError("US100 swap instrument definition changed")
            if type(row["is_close_only_mode"]) is not bool or row["market_status"] not in {"open", "closed"}:
                raise GridError("US100 swap market status changed")
            # This public endpoint is explicitly CDN cached for 60 seconds.
            # Its price is never used to execute; quotes still require <=10s.
            if source_ts is None or not _fresh(source_ts, received, 120):
                raise GridError("US100 swap metadata is stale")
            sessions = row["trading_sessions"]
            if not isinstance(sessions, list) or not sessions:
                raise GridError("US100 swap trading sessions unavailable")
            current_session = False
            closes_at = None
            for session in sessions:
                opens, closes = timestamp(session["open"]), timestamp(session["close"])
                if opens >= closes:
                    raise GridError("US100 swap trading session is invalid")
                if opens <= received < closes:
                    current_session, closes_at = True, closes
            self._market = {"symbol": VAR_SYMBOL, "display_symbol": "US100", "instrument_type": "swap",
                            "multiplier": "1", "quantity_unit": "index_unit", "mark": _text(_number(row["price"])),
                            "market_open": row["market_status"] == "open" and current_session,
                            "closes_at": closes_at, "close_only": row["is_close_only_mode"],
                            "funding_status": "missing", "funding_rate": None,
                            "dividends_status": "not_accrued", "metadata_ts": source_ts}
            self._metadata_checked = received
            return self._market
        except (KeyError, TypeError, AttributeError):
            raise GridError("US100 swap metadata schema changed") from None

    def market(self):
        market = self._metadata()
        if not market["market_open"]:
            return {**market, "ready": False, "reason": "market_closed"}
        quote = self.quote("0.01")
        return {**quote, "is_probe": True, "probe_qty": quote["qty"]}

    def quote(self, qty):
        quantity = _number(qty)
        market = self._metadata()
        if not market["market_open"] or (market["closes_at"] is not None and self.transport.clock() >= market["closes_at"]):
            raise GridError("US100 swap market is closed")
        path = "/api/quotes/simple"
        data, received, _ = self.transport.request("POST", path,
            body={"instrument": dict(VAR_INSTRUMENT), "qty": _text(quantity)})
        try:
            if data["instrument"] != VAR_INSTRUMENT or _number(data["qty"]) != quantity:
                raise GridError("US100 quote instrument or requested quantity mismatch")
            bid, ask, mark = (_number(data[name]) for name in ("bid", "ask", "mark_price"))
            if bid > ask:
                raise GridError("US100 quote is crossed")
            quote_ts = timestamp(data["timestamp"])
            if not _fresh(quote_ts, received, self.max_age_seconds):
                raise GridError("US100 indicative quote is stale")
            if market["closes_at"] is not None and received >= market["closes_at"]:
                raise GridError("US100 swap market closed during quote request")
            limits = {}
            for side in ("bid", "ask"):
                item = data["qty_limits"][side]
                lo, hi, step = (_number(item[key]) for key in ("min_qty", "max_qty", "min_qty_tick"))
                if lo > hi:
                    raise GridError("US100 quantity limits are invalid")
                limits[side] = {"min_qty": _text(lo), "max_qty": _text(hi), "size_step": _text(step)}
            if limits["bid"]["size_step"] != limits["ask"]["size_step"]:
                raise GridError("US100 quantity tick differs by side")
            step = Decimal(limits["bid"]["size_step"])
            if quantity % step:
                raise GridError("US100 requested quantity is off tick")
            minimum = max(Decimal(side["min_qty"]) for side in limits.values())
            maximum = min(Decimal(side["max_qty"]) for side in limits.values())
            if not minimum <= quantity <= maximum:
                raise GridError("US100 requested quantity is outside indicative limits")
            return {**market, "bid": _text(bid), "ask": _text(ask), "mark": _text(mark),
                    "qty": _text(quantity), "ts": quote_ts, "source_ts": quote_ts, "received_ts": received,
                    "source_time_kind": "exchange_quote_timestamp", "source": "variational_" + path.rsplit("/", 1)[1] + "_indicative",
                    "qty_limits": limits, "min_qty": _text(minimum), "max_qty": _text(maximum),
                    "size_step": _text(step), "ready": True, "reason": "", "is_probe": False}
        except (KeyError, TypeError, AttributeError, InvalidOperation):
            raise GridError("US100 indicative quote schema changed") from None
