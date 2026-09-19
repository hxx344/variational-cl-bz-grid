import argparse
import csv
from dataclasses import asdict, replace
import getpass
import json
from pathlib import Path
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from .client import Client, USER_AGENT, import_curl, save_session, token_expiry
from .engine import Engine
from .models import Config, D, GridError, HOUR, WINDOW, Quote, dec, rolling_center, utc
from .store import ProcessLock, Store


def emit(data):
    print(json.dumps(data, ensure_ascii=False, default=str), flush=True)


def configuration(path):
    path = Path(path).resolve()
    config = Config.load(path)
    resolved = replace(config, session_file=str((path.parent / config.session_file).resolve()),
                       state_file=str((path.parent / config.state_file).resolve()))
    if resolved.session_file == resolved.state_file or path in (Path(resolved.session_file), Path(resolved.state_file)):
        raise GridError("Configuration, session and ledger must use separate files")
    return resolved


def paired(function):
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(function, symbol) for symbol in ("CL", "BZ")]
        return [job.result() for job in jobs]


def init_session(args):
    config_path = Path(args.config)
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(asdict(Config(grid_step_percent="1", max_levels=30)), indent=2) + "\n", encoding="utf-8")
    config = configuration(config_path)
    if args.curl_file:
        try:
            if Path(args.curl_file).stat().st_size > 65536:
                raise GridError("Captured request is too large")
            data = import_curl(Path(args.curl_file).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError):
            raise GridError("Could not read the captured request file") from None
    else:
        data = {"token": getpass.getpass("vr-token (hidden): ").strip(), "user_agent": USER_AGENT}
    if token_expiry(data["token"]) <= time.time() + 30:
        raise GridError("Session is expired or expiring; capture a fresh session")
    save_session(config.session_file, data)
    emit(Client(config.session_file).check_session())


def check_session(args):
    config = configuration(args.config)
    emit(Client(config.session_file).check_session())


def run(args):
    config = configuration(args.config)
    client = Client(config.session_file)
    emit({"event": "session", **client.check_session()})
    with ProcessLock(config.state_file):
        store = Store(config.state_file, config)
        engine = Engine(config, store)
        cached_hour, center = None, None
        count, failures = 0, 0
        try:
            while True:
                started = time.monotonic()
                try:
                    hour_end = int(time.time()) // HOUR * HOUR
                    if hour_end != cached_hour:
                        cl_rows, bz_rows = paired(lambda s: client.candles(s, hour_end))
                        center = rolling_center(cl_rows, bz_rows, hour_end)
                        cached_hour = hour_end
                    markets = paired(client.market)
                    if not all(opened for opened, _ in markets):
                        raise GridError("CL or BZ market is closed; simulation paused")
                    cl, bz = paired(lambda s: client.quote(s, dec(config.quantity_barrels)))
                    now = time.time()
                    if int(now) // HOUR * HOUR != cached_hour:
                        raise GridError("UTC hour changed while fetching data; refresh history next poll")
                    snapshot = engine.tick(center, cl, bz, now, allow_open=not any(close_only for _, close_only in markets))
                    snapshot["history_start_utc"] = utc(cached_hour - WINDOW * HOUR)
                    snapshot["history_end_utc"] = utc(cached_hour)
                    emit(snapshot)
                    failures = 0
                except GridError as error:
                    failures += 1
                    now = time.time()
                    with store.transaction():
                        store.event(now, "paused", str(error))
                    emit({"mode": "paper", "time_utc": utc(now), "status": "paused", "reason": str(error)})
                    if args.once:
                        return 2
                count += 1
                if args.once or args.iterations and count >= args.iterations:
                    return 0 if failures == 0 else 2
                # Retry only complete read/quote cycles. No order endpoint exists in this program.
                delay = min(60, config.poll_seconds * 2 ** min(failures, 3))
                time.sleep(max(0, delay - (time.monotonic() - started)))
        finally:
            store.close()


def read_ledger(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise GridError("No ledger yet; run the simulation first")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def status(args):
    config = configuration(args.config)
    db = read_ledger(config.state_file)
    try:
        row = db.execute("SELECT ts,snapshot FROM ticks ORDER BY id DESC LIMIT 1").fetchone()
        event = db.execute("SELECT ts,kind,message FROM events ORDER BY id DESC LIMIT 1").fetchone()
        result = {"mode": "paper", "snapshot": json.loads(row["snapshot"]) if row else None,
                  "snapshot_age_seconds": max(0, round(time.time() - row["ts"])) if row else None,
                  "stale": row is None or time.time() - row["ts"] > max(60, config.poll_seconds * 3),
                  "last_event": dict(event) if event else None}
        emit(result)
    finally:
        db.close()


def export(args):
    config = configuration(args.config)
    destination = Path(args.output).resolve()
    protected = {Path(config.state_file).resolve(), Path(config.session_file).resolve(), Path(args.config).resolve()}
    if destination in protected or destination.exists():
        raise GridError("Export destination must be a new file, separate from configuration, session and ledger")
    db = read_ledger(config.state_file)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        rows = db.execute("SELECT id,lot_id,ts,phase,symbol,side,qty,price,fee FROM fills ORDER BY id")
        with destination.open("x", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["id", "lot_id", "time_utc", "phase", "symbol", "side", "quantity_barrels", "price_usdc_per_barrel", "fee_usdc"])
            for row in rows:
                values = list(row)
                values[2] = utc(values[2])
                writer.writerow(values)
        emit({"export": str(destination)})
    finally:
        db.close()


def demo(args):
    path = Path(args.state_file)
    if path.exists():
        raise GridError("Demo requires a new state_file; existing data is preserved")
    config = replace(Config(), state_file=str(path))
    base = 1735689600
    rows = lambda price: [{"unix_time_ms": t * 1000, "close": str(price)} for t in range(base - WINDOW * HOUR, base, HOUR)]
    center = rolling_center(rows(D("95")), rows(D("102")), base)
    with ProcessLock(path):
        store = Store(path, config)
        engine = Engine(config, store)
        try:
            trajectory = ["7", "7.21", "7.43", "7.65", "7.88", "7.40", "7.10", "7", "6.78", "6.55", "6.32", "6.12", "6.6", "6.9", "7"]
            for i, spread in enumerate(trajectory):
                now = base + i * 10
                def quote(symbol, mark):
                    return Quote(symbol, mark - D("0.02"), mark + D("0.02"), mark, D(1), now)
                snapshot = engine.tick(center, quote("CL", D("95")), quote("BZ", D("95") + dec(spread)), now)
                emit(snapshot)
            emit({"demo": "synthetic_scenario_not_backtest", "state_file": str(path.resolve()), "final_equity_usdc": snapshot["equity_usdc"], "open_pairs": snapshot["open_pairs"]})
        finally:
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Variational CL/BZ equal-barrel grid — PAPER ONLY")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, function in (("init-session", init_session), ("check-session", check_session), ("run", run), ("status", status), ("export", export)):
        sub = commands.add_parser(name)
        sub.add_argument("--config", default="config.local.json")
        sub.set_defaults(function=function)
        if name == "init-session":
            sub.add_argument("--curl-file", help="Local Chrome Copy as cURL (bash), GET /api/me only")
        elif name == "run":
            sub.add_argument("--once", action="store_true")
            sub.add_argument("--iterations", type=int, default=0, help="0 runs until interrupted")
        elif name == "export":
            sub.add_argument("--output", required=True)
    sub = commands.add_parser("demo")
    sub.add_argument("--state-file", default="data/demo.sqlite3")
    sub.set_defaults(function=demo)
    from .comparison import run_comparison, comparison_status, stop_comparison
    for name, function in (("compare", run_comparison), ("compare-status", comparison_status), ("compare-stop", stop_comparison)):
        sub = commands.add_parser(name)
        sub.add_argument("--experiments", default="experiments.example.json")
        sub.set_defaults(function=function)
        if name == "compare":
            sub.add_argument("--once", action="store_true")
            sub.add_argument("--iterations", type=int, default=0)
    from .dashboard import serve_dashboard
    sub = commands.add_parser("dashboard", help="Read-only web monitor through a localhost SSH tunnel")
    sub.add_argument("--experiments", default="experiments.example.json")
    sub.add_argument("--port", type=int, default=9876)
    sub.set_defaults(function=serve_dashboard)
    args = parser.parse_args(argv)
    if getattr(args, "iterations", 0) < 0:
        parser.error("iterations must be non-negative")
    try:
        return args.function(args) or 0
    except KeyboardInterrupt:
        emit({"status": "stopped", "mode": "paper", "message": "Ledger preserved; no positions were automatically closed"})
        return 0
    except GridError as error:
        emit({"status": "error", "reason": str(error)})
        return 2
    except (OSError, sqlite3.Error):
        emit({"status": "error", "reason": "Local file or ledger error; check paths, permissions and available disk space"})
        return 2
    except Exception:
        # Raw HTTP bodies, request objects and credential-bearing tracebacks must never reach logs.
        emit({"status": "error", "reason": "Unexpected failure; ledger transaction rolled back, inspect code or run tests"})
        return 2
