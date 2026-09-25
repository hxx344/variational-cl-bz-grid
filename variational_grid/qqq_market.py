"""Read-only QQQ book/trades and US100 swap indicative market data.

No signer, order, quote-acceptance or account endpoint exists here. Lighter's
REST book has no exchange timestamp/sequence; HTTP Date is labelled as such,
and a recent-trades overlap plus bounded poll interval guards simulated fills.
The first batch and every lost interval only establish a new queue anchor.
"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .client import NoRedirect, ORIGIN, USER_AGENT
from .models import GridError, timestamp
from .qqq_auth import VarSession


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
    "var_quotes": ORIGIN + "/api/quotes/indicative",
    "var_ui": ORIGIN + "/swap/US100S",
}
VAR_QUOTE_SOURCE = "variational_authenticated_indicative"


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


class RequestDeferred(GridError):
    def __init__(self, venue, path, retry_at, now, *, limited=False):
        self.venue, self.path, self.retry_at, self.limited = venue, path, retry_at, limited
        wait = max(0, math.ceil(retry_at - now))
        state = "HTTP 429 限流冷却" if limited else "报价请求排队"
        super().__init__(f"{venue} {path}：{state}，约 {wait} 秒后重试")


def retry_delay(header, now, fallback):
    """Honor both Retry-After formats; never truncate a server's longer delay."""
    try:
        value = str(header).strip()
        if value.isascii() and value.isdigit():
            delay = float(value)
        else:
            delay = parsedate_to_datetime(value).timestamp() - now
        if math.isfinite(delay) and delay > 0:
            return max(fallback, delay)
    except (ValueError, TypeError, OverflowError):
        pass
    return fallback


class _Transport:
    def __init__(self, origin, allowed, opener=None, clock=None, *, session=None):
        if session is not None and (origin != ORIGIN or not allowed <= {
                ("GET", "/api/metadata/supported_assets"), ("POST", "/api/quotes/indicative")}):
            raise GridError("Invalid authenticated market destination")
        self.origin = origin
        self.session = session
        self.allowed = allowed
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.clock = clock or time.time
        self.venue = "Variational" if origin == ORIGIN else "Lighter"
        self.retry_at, self.next_quote_at = 0.0, 0.0
        self.limited_path, self.failures = "", {}
        self.quote_interval = 3.0 if origin == ORIGIN else 0.0
        self.lock = threading.RLock()
        self._batch_remaining = None
        self._batch_probe = False

    @contextmanager
    def quote_batch(self):
        """One frame: a fresh probe may be followed by ONE exact-size quote.

        Both requests consume the time budget. The exception to spacing never
        bypasses 429 cooldown and cannot leak into the following frame.
        """
        self._batch_remaining, self._batch_probe = 2, False
        try:
            yield
        finally:
            self._batch_remaining, self._batch_probe = None, False

    def check_cooldown(self):
        now = self.clock()
        if now < self.retry_at:
            raise RequestDeferred(self.venue, self.limited_path, self.retry_at, now, limited=True)

    def state(self):
        return {"venue": self.venue, "retry_at": self.retry_at, "path": self.limited_path,
                "next_quote_at": self.next_quote_at, "quote_interval": self.quote_interval, "failures": dict(self.failures)}

    def restore(self, state):
        if not isinstance(state, dict) or state.get("venue") != self.venue:
            raise GridError("Invalid saved market cooldown")
        if self.session is not None:
            # Preserve the public route's existing cooldown when upgrading.
            old, new = "/api/quotes/simple", "/api/quotes/indicative"
            state = dict(state)
            if state.get("path") == old:
                state["path"] = new
            if isinstance(state.get("failures"), dict):
                state["failures"] = dict(state["failures"])
                if old in state["failures"]:
                    count = state["failures"].pop(old)
                    existing = state["failures"].get(new, 0)
                    if any(type(n) is not int or not 0 <= n <= 20 for n in (count, existing)):
                        raise GridError("Invalid saved cooldown counters")
                    state["failures"][new] = max(count, existing)
        for key in ("retry_at", "next_quote_at", "quote_interval"):
            value = state.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise GridError("Invalid saved market cooldown")
        path = state.get("path", "")
        failures = state.get("failures", {})
        if path and not any(p == path for _, p in self.allowed):
            raise GridError("Invalid saved cooldown route")
        if not isinstance(failures, dict) or any(p not in {p for _, p in self.allowed} or type(n) is not int or not 0 <= n <= 20 for p, n in failures.items()):
            raise GridError("Invalid saved cooldown counters")
        self.retry_at, self.next_quote_at = state["retry_at"], state["next_quote_at"]
        self.quote_interval = max(self.quote_interval, min(6, state["quote_interval"]))
        self.limited_path, self.failures = path, dict(failures)

    def request(self, method, path, *, query=None, body=None, probe=False):
        # Serialize admission with the response: queued callers cannot race past
        # a 429 received by an earlier call. No sleeping on either venue here.
        with self.lock:
            return self._request(method, path, query=query, body=body, probe=probe)

    def _request(self, method, path, *, query=None, body=None, probe=False):
        if (method, path) not in self.allowed:
            raise GridError("Endpoint is not permitted by the QQQ paper market client")
        credentials = self.session.read() if self.session else None
        self.check_cooldown()
        if method == "POST":
            now = self.clock()
            paired = self._batch_remaining == 1 and self._batch_probe and not probe
            if self._batch_remaining == 0 or (now < self.next_quote_at and not paired):
                raise RequestDeferred(self.venue, path, max(now + 1, self.next_quote_at), now)
        url = self.origin + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
                   "Cache-Control": "no-cache"}
        if self.origin == ORIGIN:
            headers["Referer"] = ORIGIN + "/"
        if credentials:
            headers["Cookie"] = "vr-token=" + credentials[0]
            headers["User-Agent"] = credentials[1]
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        requested = self.clock()
        if method == "POST":
            self.next_quote_at = max(requested, self.next_quote_at) + self.quote_interval
            if self._batch_remaining is not None:
                self._batch_remaining = 1 if probe and self._batch_remaining == 2 else 0
                self._batch_probe = probe
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
                self.failures.pop(path, None)  # A metadata GET cannot clear a quote POST's 429 streak.
                if path == self.limited_path:
                    self.retry_at, self.limited_path = 0.0, ""
                return data, received, source_ts
        except urllib.error.HTTPError as error:
            code = error.code
            retry = error.headers.get("Retry-After") if error.headers else None
            error.close()
            if code in (401, 403) and self.session:
                self.session.reject()
            if code == 429:
                now = self.clock()
                count = self.failures[path] = min(20, self.failures.get(path, 0) + 1)
                self.retry_at = now + retry_delay(retry, now, min(900, 60 * 2 ** min(count - 1, 4)))
                self.limited_path = path
                if self.origin == ORIGIN:
                    self.quote_interval = 6.0
                raise RequestDeferred(self.venue, path, self.retry_at, now, limited=True) from None
            raise GridError(f"{self.venue} {path}: Market API HTTP {code}; no simulated execution accepted") from None
        except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
            raise GridError(f"{self.venue} {path}: Market transport or JSON error; no simulated execution accepted") from None


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
Authenticated /quotes/indicative supplies quantity-specific indicative prices.
    This client cannot place or accept an order.
"""

    def __init__(self, *, session_file=None, opener=None, clock=None, max_age_seconds=10):
        self.session = VarSession(session_file)
        self.transport = _Transport(ORIGIN, {
            ("GET", "/api/metadata/supported_assets"), ("POST", "/api/quotes/indicative"),
        }, opener, clock, session=self.session)
        self.max_age_seconds = max_age_seconds
        self._market = None
        self._metadata_checked = None
        self._last_quote = None

    def _metadata(self):
        self.session.read()
        self.transport.check_cooldown()
        now = self.transport.clock()
        if (self._market is not None and self._metadata_checked is not None and 0 <= now - self._metadata_checked < 30
                and _fresh(self._market["metadata_ts"], now, 120)
                and (self._market["closes_at"] is None or now < self._market["closes_at"])):
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
            # Its price is never used to execute; quote age is checked against
            # this client's configured pricing mode independently of metadata.
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
        # Any fresh, validated US100 quote can provide the common mark/limits.
        # Preserve its original time; actual simulated hedge fills still require
        # a newly requested exact-quantity quote in that frame.
        if self.session.cache_allowed() and self._last_quote and _fresh(self._last_quote["ts"], self.transport.clock(), min(9, self.max_age_seconds)):
            return {**self._last_quote, **{k: market[k] for k in ("market_open", "closes_at", "close_only")},
                    "is_probe": True, "probe_qty": self._last_quote["qty"]}
        quote = self.quote("0.01", _probe=True)
        return {**quote, "is_probe": True, "probe_qty": quote["qty"]}

    def quote(self, qty, *, _probe=False):
        quantity = _number(qty)
        market = self._metadata()
        if not market["market_open"] or (market["closes_at"] is not None and self.transport.clock() >= market["closes_at"]):
            raise GridError("US100 swap market is closed")
        path = "/api/quotes/indicative"
        data, received, _ = self.transport.request("POST", path,
            body={"instrument": dict(VAR_INSTRUMENT), "qty": _text(quantity)}, probe=_probe)
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
            result = {**market, "bid": _text(bid), "ask": _text(ask), "mark": _text(mark),
                    "qty": _text(quantity), "ts": quote_ts, "source_ts": quote_ts, "received_ts": received,
                    "source_time_kind": "exchange_quote_timestamp", "source": VAR_QUOTE_SOURCE,
                    "qty_limits": limits, "min_qty": _text(minimum), "max_qty": _text(maximum),
                    "size_step": _text(step), "ready": True, "reason": "", "is_probe": False}
            self._last_quote = result
            self.session.accept()
            return result
        except (KeyError, TypeError, AttributeError, InvalidOperation):
            raise GridError("US100 indicative quote schema changed") from None
