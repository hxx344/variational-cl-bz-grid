"""A shared, replayable market feed and isolated paper experiments."""
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .client import Client
from .engine import Engine
from .models import Config, D, GridError, HOUR, Quote, dec, rolling_center, utc, validate_pair
from .store import ProcessLock, Store

OVERRIDES = {
    "paper_balance_usdc", "quantity_barrels", "grid_step_usdc_per_barrel", "grid_step_percent", "max_levels",
    "paper_leverage", "max_margin_fraction", "max_drawdown_fraction", "max_holding_hours",
    "slippage_bps_per_leg", "fee_bps_per_leg",
}


def quantity_key(quantity):
    return format(dec(quantity).normalize(), "f")


@dataclass
class Experiment:
    base: Config
    output: Path
    scenarios: dict

    @classmethod
    def load(cls, path):
        from .cli import configuration
        path = Path(path).resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if set(data) - {"base_config", "output_dir", "scenarios", "center_hours"} or not {"base_config", "output_dir", "scenarios"} <= set(data):
                raise ValueError()
            base = configuration(path.parent / data["base_config"])
            output = (path.parent / data["output_dir"]).resolve()
            if not isinstance(data["scenarios"], list) or not 1 <= len(data["scenarios"]) <= 20:
                raise ValueError()
            scenarios = {}
            for item in data["scenarios"]:
                name = item["name"]
                if set(item) != {"name", "overrides"} or not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,39}", name):
                    raise ValueError()
                if name.lower() in {x.lower() for x in scenarios} or not isinstance(item["overrides"], dict) or set(item["overrides"]) - OVERRIDES:
                    raise ValueError()
                state = output / "ledgers" / (name + ".sqlite3")
                overrides = dict(item["overrides"])
                # Explicit legacy absolute overrides also work with a percentage base config.
                if "grid_step_usdc_per_barrel" in overrides and "grid_step_percent" not in overrides:
                    overrides["grid_step_percent"] = None
                scenarios[name] = replace(base, **overrides, center_hours=data.get("center_hours", base.center_hours), state_file=str(state)).validate()
            if any(p.is_relative_to(output) for p in (path, Path(base.session_file), Path(base.state_file), (path.parent / data["base_config"]).resolve())):
                raise GridError("Experiment output must be separate from existing configuration, session and single-run ledger")
            return cls(base, output, scenarios)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise GridError("Invalid experiment file: require base_config, separate output_dir and 1–20 uniquely named scenarios") from None

    def identity(self):
        return {"version": 1, "max_quote_age_seconds": self.base.max_quote_age_seconds,
                "max_pair_skew_seconds": self.base.max_pair_skew_seconds,
                "scenarios": {name: json.loads(c.strategy_identity()) for name, c in self.scenarios.items()}}

    @property
    def center_hours(self):
        return next(iter(self.scenarios.values())).center_hours


@dataclass
class Frame:
    ts: float
    hour_end: int
    center: D
    quotes: dict
    allow_open: bool = True

    def validate(self, experiment):
        if self.hour_end != int(self.ts) // HOUR * HOUR or type(self.allow_open) is not bool:
            raise GridError("Invalid comparison frame hour or market status")
        dec(self.center)
        for config in experiment.scenarios.values():
            quotes = self.quotes.get(quantity_key(config.quantity_barrels))
            if quotes is None:
                raise GridError("Missing quantity-specific quote in shared frame")
            validate_pair(*quotes, self.ts, config)

    def encode(self):
        return json.dumps({"ts": self.ts, "hour_end": self.hour_end, "center": str(self.center), "allow_open": self.allow_open,
                           "quotes": {q: [asdict(x) for x in pair] for q, pair in self.quotes.items()}}, default=str)

    @classmethod
    def decode(cls, raw):
        data = json.loads(raw)
        quotes = {}
        for quantity, items in data["quotes"].items():
            quotes[quantity] = tuple(Quote(x["symbol"], dec(x["bid"]), dec(x["ask"]), dec(x["mark"]), dec(x["qty"]), x["ts"]) for x in items)
        return cls(data["ts"], data["hour_end"], dec(data["center"]), quotes, data["allow_open"])


class MarketFeed:
    def __init__(self, experiment, client=None):
        self.experiment = experiment
        self.client = client or Client(experiment.base.session_file)
        self.hour = None
        self.center = None

    def next(self):
        from .cli import paired
        hour = int(time.time()) // HOUR * HOUR
        if self.hour != hour:
            rows = paired(lambda symbol: self.client.candles(symbol, hour, self.experiment.center_hours))
            self.center = rolling_center(*rows, hour, self.experiment.center_hours)
            self.hour = hour
        markets = paired(self.client.market)
        if not all(opened for opened, _ in markets):
            raise GridError("CL or BZ market is closed; all experiments paused")
        quotes = {}
        for q in sorted({quantity_key(c.quantity_barrels) for c in self.experiment.scenarios.values()}):
            quotes[q] = tuple(paired(lambda symbol: self.client.quote(symbol, dec(q))))
        frame = Frame(time.time(), hour, self.center, quotes, not any(close_only for _, close_only in markets))
        # Validate every quantity before allowing any scenario to consume this frame.
        frame.validate(self.experiment)
        return frame


def write_json(path, data):
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class Cohort:
    def __init__(self, experiment):
        self.experiment = experiment
        self.stack = ExitStack()
        self.stores = {}
        self.engines = {}

    def __enter__(self):
        output = self.experiment.output
        try:
            self.stack.enter_context(ProcessLock(output))
            manifest = output / "experiment.json"
            if manifest.exists():
                if json.loads(manifest.read_text(encoding="utf-8")) != self.experiment.identity():
                    raise GridError("Experiments changed; choose a new output_dir to keep the comparison fair")
            else:
                if output.exists() and any(output.iterdir()):
                    raise GridError("New experiment output must be empty; existing files are preserved")
                output.mkdir(parents=True, exist_ok=True)
                write_json(manifest, self.experiment.identity())
            self.db = sqlite3.connect(output / "comparison.sqlite3")
            self.stack.callback(self.db.close)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("CREATE TABLE IF NOT EXISTS frames(ts REAL PRIMARY KEY,payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS summaries(ts REAL PRIMARY KEY,payload TEXT NOT NULL); CREATE TABLE IF NOT EXISTS runtime(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);")
            for name, config in self.experiment.scenarios.items():
                self.stack.enter_context(ProcessLock(config.state_file))
                store = Store(config.state_file, config)
                self.stack.callback(store.close)
                self.stores[name] = store
                self.engines[name] = Engine(config, store)
            from .reset import initialize, process_reset
            initialize(self.experiment)
            process_reset(self)
            self.recover()
            self.set_runtime("running")
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, exc_type, *_):
        try:
            self.set_runtime("stopped", "Simulation process stopped" if exc_type is None else "Process interrupted; resume to replay any pending shared frame")
        finally:
            self.stack.close()

    def latest(self):
        row = self.db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def set_runtime(self, status, reason=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runtime VALUES (1,?)", (json.dumps({"status": status, "reason": reason, "updated_utc": utc(time.time()), "pid": os.getpid()}),))
        self.report()

    def ingest(self, frame):
        frame.validate(self.experiment)
        last = self.db.execute("SELECT MAX(ts) FROM frames").fetchone()[0]
        if last is not None and frame.ts <= last:
            raise GridError("Shared frame must advance in time")
        # Journal first; if any scenario crashes, recover exactly this observed frame.
        with self.db:
            self.db.execute("INSERT INTO frames VALUES (?,?)", (frame.ts, frame.encode()))
        return self.apply(frame)

    def recover(self):
        latest_frame = self.db.execute("SELECT MAX(ts) FROM frames").fetchone()[0]
        summary = self.latest()
        published = 0 if summary is None else summary["ts"]
        for store in self.stores.values():
            last = store.get("last_tick")
            if last is not None and (latest_frame is None or float(last) > latest_frame):
                raise GridError("Scenario ledger was modified outside its shared experiment")
            if float(last or "0") < published:
                raise GridError("Scenario ledger is older than its published comparison; restore a consistent backup")
        floor = min(float(store.get("last_tick", "0")) for store in self.stores.values())
        # Include a frame applied to all ledgers but not yet published before a crash.
        for raw, in self.db.execute("SELECT payload FROM frames WHERE ts > ? ORDER BY ts", (min(floor, published),)).fetchall():
            self.apply(Frame.decode(raw))

    def apply(self, frame):
        snapshots = {}
        for name, engine in self.engines.items():
            store = self.stores[name]
            if float(store.get("last_tick", "0")) < frame.ts:
                snapshots[name] = engine.tick(frame.center, *frame.quotes[quantity_key(engine.config.quantity_barrels)], frame.ts, allow_open=frame.allow_open)
            else:
                snapshots[name] = store.snapshot()
                # Old versions can leave a fully applied frame unpublished at shutdown.
                if "volume_barrels" not in snapshots[name]:
                    snapshots[name].update(store.volume())
            if float(store.get("last_tick")) != frame.ts:
                raise GridError("Scenario timestamps differ; comparison not published")
        previous = self.latest()
        if previous is not None and previous["ts"] == frame.ts:
            return previous
        if previous is not None and previous["ts"] > frame.ts:
            raise GridError("Cannot publish an older comparison frame")
        previous_rows = {} if previous is None else {r["name"]: r for r in previous["scenarios"]}
        rows = []
        for name, snapshot in snapshots.items():
            config = self.experiment.scenarios[name]
            old = previous_rows.get(name, {})
            opens = sum(a["action"] == "open" for a in snapshot["actions"])
            closes = [a for a in snapshot["actions"] if a["action"] == "close"]
            wins = sum(dec(self.stores[name].db.execute("SELECT net_pnl FROM lots WHERE id=?", (a["lot_id"],)).fetchone()[0]) > 0 for a in closes)
            rows.append({**snapshot, "name": name, "grid_step": str(config.grid_step(frame.center)),
                         "grid_step_percent": config.grid_step_percent,
                         **config.grid_geometry(frame.center),
                         "quantity_barrels": config.quantity_barrels, "initial_balance_usdc": config.paper_balance_usdc,
                         "max_levels": config.max_levels, "fee_bps": config.fee_bps_per_leg, "slippage_bps": config.slippage_bps_per_leg,
                         "return_fraction": str(dec(snapshot["total_pnl_usdc"]) / dec(config.paper_balance_usdc)),
                         "opened_pairs": old.get("opened_pairs", 0) + opens, "closed_pairs": old.get("closed_pairs", 0) + len(closes),
                         "winning_pairs": old.get("winning_pairs", 0) + wins,
                         "max_drawdown_fraction": str(max(dec(old.get("max_drawdown_fraction", "0")), dec(snapshot["drawdown_fraction"]))),
                         "max_margin_usdc": str(max(dec(old.get("max_margin_usdc", "0")), dec(snapshot["margin_usdc"])))})
        result = {"mode": "paper_comparison", "ts": frame.ts, "time_utc": utc(frame.ts),
                  "started_utc": utc(frame.ts) if previous is None else previous["started_utc"],
                  "sample_count": 1 if previous is None else previous["sample_count"] + 1,
                  "history_start_utc": utc(frame.hour_end - self.experiment.center_hours * HOUR), "history_end_utc": utc(frame.hour_end),
                  "center": str(frame.center), "center_window_hours": self.experiment.center_hours, "poll_seconds": self.experiment.base.poll_seconds,
                  "pnl_basis": "before_funding", "scenarios": rows}
        with self.db:
            self.db.execute("INSERT INTO summaries VALUES (?,?)", (frame.ts, json.dumps(result)))
        self.set_runtime("running")
        return result

    def report(self):
        from .report import render_report
        row = self.db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
        runtime = json.loads(row[0]) if row else {"status": "starting"}
        summary = self.latest()
        recent = [json.loads(x[0]) for x in self.db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 360").fetchall()][::-1]
        public = self.experiment.output / "public"
        public.mkdir(exist_ok=True)
        write_json(public / "summary.json", {"runtime": runtime, "summary": summary})
        temporary = public / "index.html.new"
        temporary.write_text(render_report(summary, recent, runtime), encoding="utf-8")
        os.replace(temporary, public / "index.html")


def run_comparison(args):
    from .cli import emit
    experiment = Experiment.load(args.experiments)
    feed = MarketFeed(experiment)
    emit({"event": "session", **feed.client.check_session()})
    with Cohort(experiment) as cohort:
        stop = experiment.output / "STOP"
        if stop.exists():
            stop.unlink()  # Only the experiment's own stop sentinel; a new run resumes explicitly.
        count, failures = 0, 0
        while True:
            if stop.exists():
                stop.unlink()
                return 0
            started = time.monotonic()
            try:
                from .reset import process_reset
                if process_reset(cohort):
                    feed.hour = None
                frame = feed.next()
            except GridError as error:
                failures += 1
                cohort.set_runtime("paused", str(error))
                emit({"status": "paused", "reason": str(error)})
            else:
                result = cohort.ingest(frame)
                failures = 0
                emit({"mode": result["mode"], "time_utc": result["time_utc"], "sample_count": result["sample_count"],
                      "scenarios": [{k: r[k] for k in ("name", "total_pnl_usdc", "open_pairs", "closed_pairs", "max_drawdown_fraction", "volume_barrels", "turnover_usdc", "fill_count")} for r in result["scenarios"]]})
            count += 1
            if args.once or args.iterations and count >= args.iterations:
                return 2 if failures else 0
            delay = experiment.base.poll_seconds if not failures else min(60, experiment.base.poll_seconds * 2 ** min(failures, 3))
            deadline = time.monotonic() + max(0, delay - (time.monotonic() - started))
            while time.monotonic() < deadline and not stop.exists():
                from .reset import read_state
                if read_state(experiment)["status"] in {"pending", "archiving", "clearing"}:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))


def comparison_status(args):
    from .cli import emit
    experiment = Experiment.load(args.experiments)
    path = experiment.output / "comparison.sqlite3"
    if not path.is_file():
        raise GridError("No comparison data yet")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        summary = db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()
        runtime = db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
        summary = json.loads(summary[0]) if summary else None
        emit({"runtime": json.loads(runtime[0]) if runtime else None, "summary": summary,
              "stale": summary is None or time.time() - summary["ts"] > max(60, experiment.base.poll_seconds * 3)})


def stop_comparison(args):
    from .cli import emit
    experiment = Experiment.load(args.experiments)
    if not (experiment.output / "experiment.json").is_file():
        raise GridError("No initialized comparison to stop")
    (experiment.output / "STOP").write_text("stop\n", encoding="ascii")
    emit({"status": "stop_requested", "message": "Runner will stop after its current quote cycle; simulated positions stay saved"})
