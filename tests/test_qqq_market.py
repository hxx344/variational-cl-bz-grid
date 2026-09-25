from datetime import datetime, timezone
from email.utils import formatdate
import io
import json
import unittest
from unittest.mock import patch
import urllib.error

from variational_grid.models import GridError
from variational_grid.qqq_market import LighterClient, VarSwapClient as RealVarSwapClient, VAR_INSTRUMENT, RequestDeferred, retry_delay


def VarSwapClient(**kwargs):
    client = RealVarSwapClient(session_file="fixture-session", **kwargs)
    client.session.client.session = lambda: ("fixture.token.signature", "fixture-agent")
    return client


NOW = 1790269000


class Response(io.BytesIO):
    def __init__(self, data, now=NOW, age=None):
        super().__init__(json.dumps(data).encode())
        self.headers = {"Date": formatdate(now, usegmt=True)}
        if age is not None:
            self.headers["Age"] = str(age)


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def metadata():
    return {"code": 200, "order_book_details": [{"market_id": 129, "symbol": "QQQ", "market_type": "perp",
            "multiplier": "1", "supported_price_decimals": 2, "supported_size_decimals": 4,
            "min_base_amount": "0.0075", "min_quote_amount": "10", "mark_price": "742.05",
            "status": "active", "is_frozen": False}]}


def book():
    return {"code": 200, "bids": [{"price": "742.00", "remaining_base_amount": "3"},
                                  {"price": "742.00", "remaining_base_amount": "2"}],
            "asks": [{"price": "742.10", "remaining_base_amount": "4"}]}


def trade(identity, *, ts=NOW - 1, maker_ask=False, kind="trade"):
    return {"trade_id": identity, "trade_id_str": str(identity), "timestamp": int(ts * 1000),
            "market_id": 129, "price": "742.00", "size": "0.1", "is_maker_ask": maker_ask, "type": kind}


def frame(trades, now=NOW, depth=None):
    return [Response(metadata(), now), Response(depth or book(), now), Response({"code": 200, "trades": trades}, now)]


def var_metadata():
    return {"US100S": [{"asset": "US100S", "has_perp": True, "instrument_type": "swap", "asset_class": "index",
                        "funding_interval_s": 0, "is_close_only_mode": False, "market_status": "open", "price": "30500",
                        "trading_sessions": [{"open": "2026-09-23T22:00:00Z", "close": "2026-09-24T21:00:00Z"}]}]}


def var_quote(qty="0.01", now=NOW):
    return {"instrument": dict(VAR_INSTRUMENT), "qty": qty, "bid": "30499.5", "ask": "30500.5",
            "mark_price": "30500", "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "qty_limits": {side: {"min_qty": "0.000004", "max_qty": "32780", "min_qty_tick": "0.000001"}
                           for side in ("bid", "ask")}}


class LighterMarketTests(unittest.TestCase):
    def test_bootstrap_aggregates_depth_without_retroactive_fills(self):
        opener = Opener(frame([trade(101), trade(99)]))
        result = LighterClient(opener=opener, clock=lambda: NOW).snapshot()
        self.assertEqual(result["trades"], [])
        self.assertTrue(result["gap"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["bids"], [["742.00", "5"]])
        self.assertEqual(result["size_step"], "0.0001")
        self.assertEqual(result["min_notional"], "10")
        self.assertEqual(result["trade_cursor"], "101")
        self.assertIn("exchange_time_unavailable", result["source_time_kind"])
        self.assertTrue(all(r.method == "GET" and r.get_header("Cookie") is None for r in opener.requests))

    def test_http_cache_age_uses_maximum_not_sum_and_accounts_for_latency(self):
        # CloudFront preserves Date; Age repeats its elapsed time.
        now = [NOW - 1]
        class SlowOpener(Opener):
            def open(self, request, timeout):
                now[0] += 1
                return super().open(request, timeout)
        opener = SlowOpener([Response({}, NOW - 6, age=6)])
        client = LighterClient(opener=opener, clock=lambda: now[0])
        _, received, source = client.transport.request("GET", "/api/v1/orderBookDetails")
        self.assertEqual(received - source, 7)  # six cached seconds + one in transit
        # Cloudflare can refresh Date; Age must still protect against stale data.
        client = LighterClient(opener=Opener([Response({}, NOW, age=25)]), clock=lambda: NOW)
        _, received, source = client.transport.request("GET", "/api/v1/orderBookDetails")
        self.assertEqual(received - source, 25)

    def test_metadata_has_separate_ttl_and_mark_uses_fresh_book_mid(self):
        details = metadata()
        details["order_book_details"][0]["mark_price"] = "700"
        responses = [Response(details, NOW - 15, age=15), Response(book()), Response({"code": 200, "trades": [trade(99)]})]
        result = LighterClient(opener=Opener(responses), clock=lambda: NOW).snapshot()
        self.assertTrue(result["ready"])
        self.assertEqual(result["mark"], "742.05")
        self.assertEqual(result["mark_source"], "book_mid")
        self.assertEqual(result["venue_mark"], "700")
        self.assertEqual(result["venue_mark_ts"], NOW - 15)

    def test_metadata_ttl_never_relaxes_book_or_trade_response_freshness(self):
        for position, expected in [(0, "metadata"), (1, "book"), (2, "trades")]:
            responses = frame([trade(99)])
            responses[position].headers["Date"] = formatdate(NOW - (31 if position == 0 else 11), usegmt=True)
            with self.subTest(source=expected):
                result = LighterClient(opener=Opener(responses), clock=lambda: NOW).snapshot()
                self.assertFalse(result["ready"])
                self.assertIn(expected, result["stale_sources"])
                self.assertEqual(result["trades"], [])

    def test_overlap_orders_new_trades_maps_aggressor_and_deduplicates(self):
        opener = Opener(frame([trade(99)]) + frame([trade(110, maker_ask=True), trade(101), trade(99)]) + frame([trade(110), trade(101), trade(99)]))
        client = LighterClient(opener=opener, clock=lambda: NOW)
        client.snapshot()
        result = client.snapshot()
        self.assertFalse(result["gap"])
        self.assertEqual([(r["id"], r["side"]) for r in result["trades"]], [("101", "sell"), ("110", "buy")])
        self.assertEqual(client.snapshot()["trades"], [])

    def test_window_overflow_reanchors_and_next_overlap_recovers(self):
        opener = Opener(frame([trade(99)]) + frame([trade(400), trade(300)]) + frame([trade(401), trade(400)]))
        client = LighterClient(opener=opener, clock=lambda: NOW)
        client.snapshot()
        result = client.snapshot()
        self.assertTrue(result["gap"])
        self.assertEqual(result["reason"], "trade_window_gap")
        self.assertEqual(result["trades"], [])
        self.assertEqual([t["id"] for t in client.snapshot()["trades"]], ["401"])

    def test_poll_gap_and_transport_failure_do_not_backfill(self):
        now = [NOW]
        opener = Opener(frame([trade(99)]) + [OSError("secret-response")] + frame([trade(100), trade(99)])
                        + frame([trade(101, ts=NOW + 30), trade(100)], now=NOW + 30))
        client = LighterClient(opener=opener, clock=lambda: now[0])
        client.snapshot()
        with self.assertRaisesRegex(GridError, "transport") as caught:
            client.snapshot()
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(client.snapshot()["trades"], [])
        now[0] += 30
        self.assertEqual(client.snapshot()["reason"], "poll_gap")

    def test_stale_http_stale_trade_and_future_trade_cannot_fill(self):
        for new_trade, second_time in [(trade(100, ts=NOW - 30), NOW), (trade(100, ts=NOW + 8), NOW)]:
            with self.subTest(new_trade=new_trade):
                client = LighterClient(opener=Opener(frame([trade(99, ts=NOW - 40)]) + frame([new_trade, trade(99, ts=NOW - 40)])), clock=lambda: NOW)
                client.snapshot()
                self.assertEqual(client.snapshot()["trades"], [])
        client = LighterClient(opener=Opener(frame([trade(99)], NOW - 30)), clock=lambda: NOW)
        result = client.snapshot()
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "stale_http_source")

    def test_malformed_or_crossed_market_is_rejected(self):
        for mutate in (lambda d: d["bids"][0].update(price="NaN"), lambda d: d["asks"][0].update(price="741")):
            data = book()
            mutate(data)
            with self.assertRaises(GridError):
                LighterClient(opener=Opener(frame([trade(99)], depth=data)), clock=lambda: NOW).snapshot()
        for mutate in (lambda t: t.update(is_maker_ask="false"), lambda t: t.update(timestamp=NOW),
                       lambda t: t.update(trade_id_str="other"), lambda t: t.update(market_id=0)):
            data = trade(99)
            mutate(data)
            with self.assertRaises(GridError):
                LighterClient(opener=Opener(frame([data])), clock=lambda: NOW).snapshot()

    def test_nonbook_settlement_events_do_not_consume_maker_queue(self):
        client = LighterClient(opener=Opener(frame([trade(99)]) + frame([trade(100, kind="market-settlement"), trade(99)])), clock=lambda: NOW)
        client.snapshot()
        self.assertEqual(client.snapshot()["trades"], [])

    def test_empty_bootstrap_and_regressed_window_never_replay_history(self):
        client = LighterClient(opener=Opener(frame([]) + frame([trade(100)]) + frame([trade(99)])
                                           + frame([trade(102), trade(100)]) + frame([trade(103), trade(102)])), clock=lambda: NOW)
        self.assertEqual(client.snapshot()["trades"], [])
        self.assertEqual(client.snapshot()["trades"], [])
        regressed = client.snapshot()
        self.assertEqual(regressed["reason"], "trade_window_regressed")
        self.assertEqual(regressed["trade_cursor"], "100")
        self.assertEqual(client.snapshot()["trades"], [])
        self.assertEqual([t["id"] for t in client.snapshot()["trades"]], ["103"])


class VarSwapMarketTests(unittest.TestCase):
    def test_delayed_quotes_cannot_starve_exact_size_behind_repeated_probes(self):
        now = [NOW]
        posts = []
        class DelayedQuoteOpener:
            def open(self, request, timeout):
                if request.method == "GET":
                    return Response(var_metadata(), now[0])
                qty = json.loads(request.data)["qty"]
                posts.append((now[0] - NOW, qty))
                return Response(var_quote(qty, now[0] - 4), now[0])
        client = VarSwapClient(opener=DelayedQuoteOpener(), clock=lambda: now[0])
        client.transport.quote_interval = 6  # Post-429 pace, quote timestamp lags four seconds.
        for elapsed in range(0, 33, 2):
            now[0] = NOW + elapsed
            with client.transport.quote_batch():
                try:
                    client.market()
                    client.quote("0.02")
                    with self.assertRaises(RequestDeferred):
                        client.quote("0.03")
                except RequestDeferred:
                    pass
        self.assertEqual(posts, [(0, "0.01"), (0, "0.02"), (12, "0.01"), (12, "0.02"), (24, "0.01"), (24, "0.02")])
        # Batch privilege ends even when the block exits via an exception.
        now[0] = NOW + 34
        with self.assertRaises(RequestDeferred):
            client.quote("0.03")

    def test_slow_execution_response_cannot_admit_more_sizes_in_same_frame(self):
        now = [NOW]
        posts = []
        class SlowQuoteOpener:
            def open(self, request, timeout):
                if request.method == "GET":
                    return Response(var_metadata(), now[0])
                qty = json.loads(request.data)["qty"]
                posts.append(qty)
                if qty != "0.01":
                    now[0] += 6
                return Response(var_quote(qty, now[0]), now[0])
        client = VarSwapClient(opener=SlowQuoteOpener(), clock=lambda: now[0])
        client.market()
        now[0] += 4
        with client.transport.quote_batch():
            self.assertEqual(client.market()["ts"], NOW)
            self.assertEqual(client.quote("0.02")["ts"], NOW + 10)
            with self.assertRaises(RequestDeferred):
                client.quote("0.03")
        self.assertEqual(posts, ["0.01", "0.02"])

    def test_probe_cache_preserves_timestamp_and_execution_always_refetches(self):
        now = [NOW]
        opener = Opener([Response(var_metadata()), Response(var_quote()), Response(var_quote("0.02", NOW + 3)),
                         Response(var_quote(now=NOW + 13))])
        client = VarSwapClient(opener=opener, clock=lambda: now[0])
        client.market()
        now[0] += 2
        self.assertEqual(client.market()["ts"], NOW)
        with self.assertRaises(RequestDeferred) as caught:
            client.quote("0.01")  # Cached probe is not a new execution price.
        self.assertFalse(caught.exception.limited)
        self.assertEqual(len(opener.requests), 2)
        now[0] += 1
        self.assertEqual(client.quote("0.02")["ts"], NOW + 3)
        now[0] = NOW + 10
        self.assertEqual(client.market()["ts"], NOW + 3)
        now[0] = NOW + 13
        self.assertEqual(client.market()["ts"], NOW + 13)
        self.assertEqual([r.method for r in opener.requests], ["GET", "POST", "POST", "POST"])

    def test_metadata_cache_respects_source_age_and_session_close(self):
        now = [NOW]
        closing = var_metadata()
        closing["US100S"][0]["trading_sessions"][0]["close"] = datetime.fromtimestamp(NOW + 4, timezone.utc).isoformat()
        opener = Opener([Response(closing), Response(var_quote()), Response(closing, NOW + 4)])
        client = VarSwapClient(opener=opener, clock=lambda: now[0])
        client.market()
        now[0] += 4
        self.assertFalse(client.market()["market_open"])
        self.assertEqual([r.method for r in opener.requests], ["GET", "POST", "GET"])
        # Local cache age alone cannot prolong the source's 120-second TTL.
        now[0] = NOW
        opener = Opener([Response(var_metadata(), NOW - 119), Response(var_quote()),
                         Response(var_metadata(), NOW + 2)])
        client = VarSwapClient(opener=opener, clock=lambda: now[0])
        client.market()
        now[0] += 2
        client.market()
        self.assertEqual([r.method for r in opener.requests], ["GET", "POST", "GET"])

    def test_authenticated_probe_uses_real_swap_units_and_saved_session(self):
        opener = Opener([Response(var_metadata()), Response(var_quote())])
        result = VarSwapClient(opener=opener, clock=lambda: NOW).market()
        self.assertTrue(result["is_probe"])
        self.assertEqual(result["symbol"], "US100S")
        self.assertEqual(result["multiplier"], "1")
        self.assertEqual(result["size_step"], "0.000001")
        self.assertEqual(result["funding_status"], "missing")
        request = opener.requests[-1]
        self.assertEqual(request.full_url, "https://omni.variational.io/api/quotes/indicative")
        self.assertEqual(json.loads(request.data)["instrument"], VAR_INSTRUMENT)
        self.assertEqual(request.get_header("Cookie"), "vr-token=fixture.token.signature")

    def test_exact_requested_quantity_and_direction_limits(self):
        data = var_quote("0.032778")
        data["qty_limits"]["ask"]["min_qty"] = "0.000005"
        client = VarSwapClient(opener=Opener([Response(var_metadata()), Response(data)]), clock=lambda: NOW)
        result = client.quote("0.032778")
        self.assertEqual(result["qty"], "0.032778")
        self.assertEqual(result["min_qty"], "0.000005")
        self.assertFalse(result["is_probe"])
        self.assertEqual(result["ts"], NOW)

    def test_wrong_qty_instrument_stale_quote_and_invalid_limits_rejected(self):
        mutations = [lambda d: d.update(qty="0.02"), lambda d: d["instrument"].update(underlying="US100"),
                     lambda d: d.update(timestamp="2020-01-01T00:00:00Z"), lambda d: d.update(bid="NaN"),
                     lambda d: d["qty_limits"]["bid"].update(min_qty_tick="0"),
                     lambda d: d["qty_limits"]["ask"].update(max_qty="0.005")]
        for mutation in mutations:
            data = var_quote()
            mutation(data)
            with self.subTest(mutation=mutation), self.assertRaises(GridError):
                VarSwapClient(opener=Opener([Response(var_metadata()), Response(data)]), clock=lambda: NOW).quote("0.01")

    def test_closed_market_never_requests_a_quote(self):
        data = var_metadata()
        data["US100S"][0]["market_status"] = "closed"
        opener = Opener([Response(data)])
        client = VarSwapClient(opener=opener, clock=lambda: NOW)
        self.assertFalse(client.market()["ready"])
        with self.assertRaisesRegex(GridError, "closed"):
            client.quote("0.01")
        self.assertEqual(len(opener.requests), 1)

    def test_cached_open_flag_outside_trading_hours_is_closed(self):
        data = var_metadata()
        data["US100S"][0]["trading_sessions"] = [{"open": "2026-09-24T22:00:00Z", "close": "2026-09-25T21:00:00Z"}]
        client = VarSwapClient(opener=Opener([Response(data)]), clock=lambda: NOW)
        self.assertFalse(client.market()["market_open"])

    def test_cdn_metadata_cache_does_not_relax_quote_age(self):
        client = VarSwapClient(opener=Opener([Response(var_metadata(), age=50), Response(var_quote())]), clock=lambda: NOW)
        self.assertTrue(client.market()["ready"])
        client = VarSwapClient(opener=Opener([Response(var_metadata(), age=50), Response(var_quote(now=NOW - 15))]), clock=lambda: NOW)
        with self.assertRaisesRegex(GridError, "quote is stale"):
            client.market()

    def test_fixed_routes_and_redirect_rejection_prevent_credential_escape(self):
        opener = Opener([])
        client = VarSwapClient(opener=opener, clock=lambda: NOW)
        for method, path in [("POST", "/api/quotes/accept"), ("POST", "/api/quotes/simple"), ("POST", "/api/orders/new/market"),
                             ("GET", "https://evil.invalid"), ("GET", "/api/me")]:
            with self.assertRaises(GridError):
                client.transport.request(method, path)
        self.assertEqual(opener.requests, [])
        error = urllib.error.HTTPError("https://omni.variational.io/", 302, "secret-response", {}, io.BytesIO(b"secret-response"))
        client = VarSwapClient(opener=Opener([error]), clock=lambda: NOW)
        with self.assertRaisesRegex(GridError, "HTTP 302") as caught:
            client.market()
        self.assertNotIn("secret", str(caught.exception))

class MarketRateLimitTests(unittest.TestCase):
    @staticmethod
    def error(retry=None):
        return urllib.error.HTTPError("https://omni.variational.io/api/quotes/indicative", 429, "private body",
                                      {} if retry is None else {"Retry-After": retry}, io.BytesIO(b"private body"))

    def test_retry_after_seconds_date_and_fallback(self):
        for header, expected in [("1800", 1800), (formatdate(NOW + 3600, usegmt=True), 3600),
                                 (None, 60), ("garbage", 60), ("-3", 60), ("0", 60),
                                 (formatdate(NOW - 10, usegmt=True), 60), ("5", 60)]:
            with self.subTest(header=header):
                self.assertEqual(retry_delay(header, NOW, 60), expected)

    def test_quote_429_stops_all_origin_calls_but_not_other_venue(self):
        now = [NOW]
        error = self.error("1800")
        opener = Opener([error, Response({}, NOW + 1800)])
        transport = VarSwapClient(opener=opener, clock=lambda: now[0]).transport
        with self.assertRaisesRegex(RequestDeferred, "Variational.*HTTP 429") as caught:
            transport.request("POST", "/api/quotes/indicative", body={})
        self.assertNotIn("private", str(caught.exception))
        for offset in (2, 60, 1799):
            now[0] = NOW + offset
            for method, path in [("POST", "/api/quotes/indicative"), ("GET", "/api/metadata/supported_assets")]:
                with self.assertRaises(RequestDeferred):
                    transport.request(method, path)
        self.assertEqual(len(opener.requests), 1)
        lighter = LighterClient(opener=Opener([Response({})]), clock=lambda: NOW).transport
        lighter.request("GET", "/api/v1/orderBookDetails")
        now[0] = NOW + 1800
        transport.request("POST", "/api/quotes/indicative", body={})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(transport.failures, {})
        self.assertEqual(transport.retry_at, 0)
        self.assertEqual(transport.quote_interval, 6)

    def test_metadata_success_cannot_reset_quote_backoff_and_restart_restores_it(self):
        now = [NOW]
        opener = Opener([self.error(), Response({}, NOW + 60), self.error()])
        transport = VarSwapClient(opener=opener, clock=lambda: now[0]).transport
        with self.assertRaises(RequestDeferred):
            transport.request("POST", "/api/quotes/indicative")
        now[0] += 60
        transport.request("GET", "/api/metadata/supported_assets")
        with self.assertRaises(RequestDeferred) as caught:
            transport.request("POST", "/api/quotes/indicative")
        self.assertEqual(caught.exception.retry_at, NOW + 180)
        restored_opener = Opener([])
        restored = VarSwapClient(opener=restored_opener, clock=lambda: now[0]).transport
        restored.restore(json.loads(json.dumps(transport.state())))
        with self.assertRaises(RequestDeferred):
            restored.request("GET", "/api/metadata/supported_assets")
        self.assertEqual(restored_opener.requests, [])
        self.assertEqual(restored.failures["/api/quotes/indicative"], 2)
        with self.assertRaises(GridError):
            restored.restore({"venue": "Variational"})

    def test_lighter_429_gates_book_and_trade_requests(self):
        opener = Opener([self.error()])
        client = LighterClient(opener=opener, clock=lambda: NOW)
        for _ in range(3):
            with self.assertRaisesRegex(RequestDeferred, "Lighter.*429"):
                client.snapshot()
        self.assertEqual(len(opener.requests), 1)

    def test_probe_batch_credit_never_bypasses_origin_cooldown(self):
        opener = Opener([self.error()])
        transport = VarSwapClient(opener=opener, clock=lambda: NOW).transport
        with transport.quote_batch():
            with self.assertRaises(RequestDeferred):
                transport.request("POST", "/api/quotes/indicative", probe=True)
            with self.assertRaises(RequestDeferred) as caught:
                transport.request("POST", "/api/quotes/indicative")
            self.assertTrue(caught.exception.limited)
        self.assertEqual(len(opener.requests), 1)


if __name__ == "__main__":
    unittest.main()
