import base64
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from variational_grid.client import Client, ME, NoRedirect, import_curl, save_session, token_expiry
from variational_grid.engine import Engine
from variational_grid.models import Config, D, GridError, HOUR, WINDOW, Quote, rolling_center, validate_pair
from variational_grid.store import ProcessLock, Store


def token(exp=None):
    claims = json.dumps({"exp": exp or int(time.time()) + 3600}).encode()
    return "e30." + base64.urlsafe_b64encode(claims).decode().rstrip("=") + ".test"


def pair(spread="7", now=1000, qty="1", width="0.02"):
    def q(symbol, mark):
        return Quote(symbol, mark - D(width), mark + D(width), mark, D(qty), now)
    return q("CL", D(95)), q("BZ", D(95) + D(spread))


class HistoryTests(unittest.TestCase):
    end = 1735689600

    def rows(self, symbol):
        # Vary both series. Mean of paired spread must equal 7 + 83.5 / 100.
        return [{"unix_time_ms": (self.end - (168 - i) * HOUR) * 1000,
                 "close": str(D(95) + D(i) / 10 + (D(7) + D(i) / 100 if symbol == "BZ" else 0))} for i in range(168)]

    def test_exact_aligned_mean_ignores_open_candle(self):
        cl, bz = self.rows("CL"), self.rows("BZ")
        bz.append({"unix_time_ms": self.end * 1000, "close": "99999"})
        self.assertEqual(rolling_center(list(reversed(cl)), bz, self.end), D("7.835"))

    def test_missing_duplicate_nan_misaligned_fail(self):
        for operation in (lambda r: r.pop(10), lambda r: r.append(r[1]),
                          lambda r: r[0].update(close="NaN"), lambda r: r[0].update(unix_time_ms=1)):
            with self.subTest(operation=operation):
                cl, bz = self.rows("CL"), self.rows("BZ")
                operation(bz)
                with self.assertRaises(GridError):
                    rolling_center(cl, bz, self.end)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "paper.sqlite3")
        self.config = Config()
        self.store = Store(self.path, self.config)
        self.engine = Engine(self.config, self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def tick(self, spread, now=1000, center="7", **kwargs):
        return self.engine.tick(D(center), *pair(spread, now), now, **kwargs)

    def test_neutral_below_threshold(self):
        result = self.tick("7.19")
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["equity_usdc"], "1000")

    def test_short_spread_equal_barrels_and_market_sides(self):
        result = self.tick("7.21")
        self.assertEqual((result["cl_barrels"], result["bz_barrels"]), ("1", "-1"))
        fills = self.store.db.execute("SELECT symbol,side,price FROM fills ORDER BY id").fetchall()
        self.assertEqual([(r[0], r[1]) for r in fills], [("CL", "buy"), ("BZ", "sell")])
        self.assertEqual(D(fills[0][2]), D("95.02") * D("1.0001"))
        self.assertLess(D(result["equity_usdc"]), D(1000))

    def test_long_spread_profit_after_four_costs(self):
        self.tick("6.78")
        no_exit = self.tick("7.00", 1010)
        self.assertEqual(no_exit["open_pairs"], 1)  # A mark move of 0.22 is not a net 0.20 profit.
        result = self.tick("7.20", 1020)
        self.assertEqual(result["open_pairs"], 0)
        self.assertEqual(result["actions"][0]["reason"], "take_profit")
        self.assertGreaterEqual(D(result["realized_pnl_usdc"]), D("0.20"))

    def test_short_spread_profit(self):
        self.tick("7.22")
        result = self.tick("6.80", 1010)
        self.assertEqual(result["open_pairs"], 0)
        self.assertGreaterEqual(D(result["realized_pnl_usdc"]), D("0.20"))

    def test_center_drift_does_not_force_losing_exit(self):
        self.tick("7.4")
        result = self.tick("7.4", 1010, center="8")
        self.assertEqual(result["open_pairs"], 1)
        self.assertEqual(result["skip_reason"], "opposite_inventory")
        self.assertEqual(self.store.lots()[0]["entry_center"], "7")

    def test_gap_adds_one_pair_per_tick_with_max_levels(self):
        for i in range(12):
            result = self.tick("12", 1000 + 10 * i)
            self.assertLessEqual(len(result["actions"]), 1)
        self.assertEqual(result["open_pairs"], 8)
        self.assertEqual(D(result["cl_barrels"]) + D(result["bz_barrels"]), 0)

    def test_restart_keeps_slots_and_cash(self):
        first = self.tick("7.21")
        self.store.close()
        self.store = Store(self.path, self.config)
        self.engine = Engine(self.config, self.store)
        second = self.tick("7.21", 1010)
        self.assertEqual(second["open_pairs"], 1)
        self.assertEqual(second["equity_usdc"], first["equity_usdc"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 2)

    def test_out_of_order_tick_has_no_side_effect(self):
        before = self.tick("7.21")
        with self.assertRaises(GridError):
            self.tick("7.9", 1000)
        self.assertEqual(self.store.snapshot(), before)

    def test_stale_and_skewed_quotes_never_mutate_ledger(self):
        for cl, bz, now in ((*pair("7.5"), 1020), (pair("7.5")[0], replace(pair("7.5")[1], ts=990), 1000)):
            with self.assertRaises(GridError):
                self.engine.tick(D(7), cl, bz, now)
        self.assertEqual(self.store.lots(), [])
        self.assertIsNone(self.store.snapshot())

    def test_simulated_leg_write_failure_rolls_back_both(self):
        self.store.db.execute("CREATE TRIGGER fail_bz BEFORE INSERT ON fills WHEN NEW.symbol='BZ' BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(Exception):
            self.tick("7.21")
        self.assertEqual(self.store.lots(), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
        self.assertEqual(self.store.get("cash"), "1000")

    def test_economic_config_change_rejected(self):
        self.tick("7.21")
        with self.assertRaises(GridError):
            Store(self.path, replace(self.config, quantity_barrels="2"))

    def test_close_only_allows_exit_but_no_entry(self):
        self.assertEqual(self.tick("7.8", allow_open=False)["open_pairs"], 0)
        self.tick("7.8", 1010)
        result = self.tick("7.3", 1020, allow_open=False)
        self.assertEqual(result["open_pairs"], 0)

    def test_reentry_requires_rearming(self):
        self.tick("7.8")  # slot 1
        self.tick("7.4", 1010)  # take profit but still above slot 1 threshold
        result = self.tick("7.4", 1020)
        self.assertEqual(result["actions"][0]["level"], 2)  # no slot 1 churn
        self.tick("7.0", 1030)
        result = self.tick("7.21", 1040)
        self.assertEqual(result["actions"][0]["level"], 1)

    def test_margin_budget_and_drawdown_latch(self):
        self.store.close()
        self.path = str(Path(self.temp.name) / "small.sqlite3")
        self.config = replace(Config(), paper_balance_usdc="100", max_levels=8)
        self.store = Store(self.path, self.config)
        self.engine = Engine(self.config, self.store)
        self.tick("9")
        self.tick("9", 1010)
        result = self.tick("9", 1020)
        self.assertEqual(result["skip_reason"], "margin_budget")
        result = self.tick("30", 1030)
        self.assertEqual(result["halted"], "max_drawdown")
        self.assertEqual(result["open_pairs"], 0)
        self.assertEqual(self.tick("7.3", 1040)["actions"], [])

    def test_max_holding_exits_at_loss(self):
        self.tick("7.21")
        result = self.tick("7.4", 1000 + 168 * HOUR)
        self.assertEqual(result["actions"][0]["reason"], "max_holding")
        self.assertLess(D(result["realized_pnl_usdc"]), 0)

    def test_fees_match_fills_and_equity(self):
        self.store.close()
        self.path = str(Path(self.temp.name) / "fees.sqlite3")
        self.config = replace(Config(), fee_bps_per_leg="5")
        self.store = Store(self.path, self.config)
        self.engine = Engine(self.config, self.store)
        self.tick("7.3")
        result = self.tick("6.5", 1010)
        fees = sum((D(r[0]) for r in self.store.db.execute("SELECT fee FROM fills")), D(0))
        self.assertEqual(D(result["fees_usdc"]), fees)
        self.assertEqual(D(result["equity_usdc"]) - 1000, D(result["realized_pnl_usdc"]))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 4)

    def test_single_writer(self):
        with ProcessLock(self.path):
            with self.assertRaises(GridError):
                with ProcessLock(self.path):
                    self.fail("second writer acquired lock")


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "session.json"
        save_session(self.path, {"token": token()})

    def tearDown(self):
        self.temp.cleanup()

    def test_import_minimizes_credentials(self):
        value = token()
        parsed = import_curl(f"curl '{ME}' -b 'tracking=secret; vr-token={value}' -H 'x-unused: data' --compressed")
        self.assertEqual(parsed["token"], value)
        self.assertEqual(set(parsed), {"token", "user_agent"})

    def test_curl_rejects_foreign_host_commands_and_writes(self):
        for text in (f"curl '{ME}'; touch stolen", "curl 'https://evil.invalid/api/me'",
                     f"curl '{ME}' -X POST", f"curl '{ME}' --output x", f"curl '{ME}' --data x"):
            with self.assertRaises(GridError):
                import_curl(text)

    def test_invalid_expired_and_header_injected_tokens(self):
        for value in ("invalid", token() + "\r\nBad: header", "e30.e30.test"):
            with self.assertRaises(GridError):
                token_expiry(value)
        save_session(self.path, {"token": token(100)})
        with self.assertRaises(GridError):
            Client(self.path).session()

    def test_no_order_or_arbitrary_url_can_reach_transport(self):
        class Opener:
            def open(self, *args, **kwargs):
                raise AssertionError("network must not be reached")
        client = Client(self.path, Opener())
        for method, endpoint in (("POST", "/orders/new/market"), ("GET", "https://evil.invalid"), ("POST", "/auth/login"), ("GET", "/candles/../orders")):
            with self.assertRaises(GridError):
                client.request(method, endpoint)

    def test_transport_has_only_selected_cookie_and_fixed_origin(self):
        captured = []
        class Response(io.BytesIO):
            pass
        class Opener:
            def open(self, req, timeout):
                captured.append(req)
                return Response(b'{"ok":true}')
        Client(self.path, Opener()).request("GET", "/candles", query={"cex_asset": "CL&redirect=https://evil.invalid"})
        req = captured[0]
        self.assertTrue(req.full_url.startswith("https://omni.variational.io/api/candles?"))
        self.assertNotIn("&redirect=", req.full_url)
        self.assertTrue(req.get_header("Cookie").startswith("vr-token="))
        self.assertIsNone(req.get_header("Authorization"))

    def test_redirect_is_not_followed_and_error_body_not_logged(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid"))
        class Opener:
            def open(self, req, timeout):
                raise urllib.error.HTTPError(req.full_url, 302, "SECRET", {}, io.BytesIO(b"SECRET"))
        with self.assertRaises(GridError) as error:
            Client(self.path, Opener()).request("GET", "/me")
        self.assertNotIn("SECRET", str(error.exception))

    def test_world_readable_session_refused_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX permission contract")
        self.path.chmod(0o644)
        with self.assertRaises(GridError):
            Client(self.path).session()


class QuoteTests(unittest.TestCase):
    def payload(self):
        return {"instrument": {"underlying": "CL", "instrument_type": "perpetual_rwa_future", "settlement_asset": "USDC", "kind": "commodity"},
                "qty": "1", "bid": "95", "ask": "95.04", "mark_price": "95.02", "timestamp": "2026-09-19T08:00:00Z",
                "qty_limits": {side: {"min_qty_tick": "0.001", "min_qty": "0.002", "max_qty": "100"} for side in ("bid", "ask")}}

    def test_instrument_and_quantity_contract(self):
        self.assertEqual(Quote.parse("CL", D(1), self.payload()).symbol, "CL")
        for transform in (lambda p: p.update(qty="2"), lambda p: p.update(ask="94"),
                          lambda p: p["instrument"].update(underlying="BTC"),
                          lambda p: p["qty_limits"]["bid"].update(max_qty="0.5"),
                          lambda p: p["qty_limits"]["ask"].update(min_qty_tick="0.3")):
            payload = self.payload()
            transform(payload)
            with self.assertRaises(GridError):
                Quote.parse("CL", D(1), payload)

    def test_live_mode_and_nonfinite_settings_rejected(self):
        for config in (replace(Config(), mode="live"), replace(Config(), quantity_barrels="NaN"), replace(Config(), max_levels=True)):
            with self.assertRaises(GridError):
                config.validate()


if __name__ == "__main__":
    unittest.main()
