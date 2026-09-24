"""Shared, journaled observations for inventory-band paper comparisons."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_CEILING
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .client import Client
from .comparison import Cohort, write_json
from .inventory import (SYMBOLS, InventorySettings, InventoryConfig, InventoryStore, InventoryEngine,
                        encoded, empty_signals, signal_step, quote_key, dec, digest)
from .models import D, GridError, HOUR, Quote, rolling_center, utc


@dataclass
class InventoryExperiment:
    base: object
    output: Path
    scenarios: dict
    settings: InventorySettings
    kind = "inventory"
    center_hours = 72

    @classmethod
    def load(cls, path):
        from .cli import configuration
        path = Path(path).resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if set(data) != {"kind", "base_config", "output_dir", "strategy", "scenarios"} or data["kind"] != "inventory":
                raise ValueError()
            base_path = (path.parent / data["base_config"]).resolve()
            base = replace(configuration(base_path), center_hours=72, max_levels=None, max_margin_fraction=None)
            output = (path.parent / data["output_dir"]).resolve()
            settings = InventorySettings(**data["strategy"]).validate()
            if dec(base.quantity_barrels) % dec(settings.execution_step_barrels):
                raise GridError("Base quantity must be a multiple of execution_step_barrels")
            scenarios = {}
            if not isinstance(data["scenarios"], list) or not 2 <= len(data["scenarios"]) <= 20:
                raise ValueError()
            for item in data["scenarios"]:
                if set(item) != {"name", "tolerance_percent"}:
                    raise ValueError()
                name, limit = item["name"], item["tolerance_percent"]
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,39}", name):
                    raise ValueError()
                if name.lower() in {x.lower() for x in scenarios}:
                    raise ValueError()
                if limit is not None:
                    if not 0 <= dec(limit) < 100:
                        raise GridError("Inventory tolerance must be in [0, 100), or null for no control")
                    limit = str(dec(limit))
                scenarios[name] = InventoryConfig(base, settings, name, limit, str(output / "ledgers" / (name + ".sqlite3")))
            if len({s.tolerance_percent for s in scenarios.values()}) != len(scenarios):
                raise GridError("Use distinct inventory tolerances for a comparison")
            if any(p.is_relative_to(output) for p in (path, base_path, Path(base.session_file), Path(base.state_file))):
                raise GridError("Inventory output must be separate from configuration, session and existing ledger")
            return cls(base, output, scenarios, settings)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise GridError("Invalid inventory experiment: require kind, base_config, separate output_dir, strategy and 2–20 unique scenarios") from None

    def identity(self):
        return {"kind": self.kind, "version": 1, "max_quote_age_seconds": self.base.max_quote_age_seconds,
                "max_pair_skew_seconds": self.base.max_pair_skew_seconds,
                "scenarios": {name: json.loads(c.strategy_identity()) for name, c in self.scenarios.items()}}


@dataclass
class InventoryFrame:
    ts: float
    hour_end: int
    centers: dict
    marks: dict
    quotes: dict
    signals: dict
    raw: dict
    components: dict
    plans: dict
    allow_open: bool = True
    data_kind: str = "live_indicative"
    execution_step: str = "0.01"

    def encode(self):
        return encoded(asdict(self))

    @classmethod
    def decode(cls, raw):
        return cls(**json.loads(raw))

    def validate(self, experiment):
        if not math.isfinite(self.ts) or self.hour_end != int(self.ts) // HOUR * HOUR:
            raise GridError("Inventory frame crossed an hour boundary")
        if set(self.plans) != set(experiment.scenarios) or type(self.allow_open) is not bool:
            raise GridError("Incomplete inventory scenario plans")
        if self.data_kind not in {"live_indicative", "synthetic"} or dec(self.execution_step) <= 0:
            raise GridError("Invalid inventory frame source or quantity step")
        for symbol in SYMBOLS:
            if dec(self.marks[symbol]) <= 0 or dec(self.centers[symbol]) <= 0:
                raise GridError("Invalid shared inventory mark or center")
        if abs(dec(self.centers["BZ"]) - dec(self.centers["CL"]) - dec(self.centers["spread"])) > D("1e-20"):
            raise GridError("Inventory centers do not align")
        timestamps = []
        for key, quote in self.quotes.items():
            quantity = dec(quote["qty"])
            if key != quote_key(quote["symbol"], quantity) or quote["symbol"] not in SYMBOLS or quantity <= 0:
                raise GridError("Inventory quote quantity does not match its key")
            if dec(quote["bid"]) <= 0 or dec(quote["ask"]) < dec(quote["bid"]) or dec(quote["mark"]) <= 0:
                raise GridError("Invalid inventory quote prices")
            timestamp = quote["ts"]
            if not math.isfinite(timestamp) or timestamp > self.ts + 2 or self.ts - timestamp > experiment.base.max_quote_age_seconds:
                raise GridError("Inventory quote is stale or from the future; all accounts paused")
            timestamps.append(timestamp)
            limits = quote.get("limits")
            if limits:
                if quantity % dec(limits["tick"]) or not dec(limits["minimum"]) <= quantity <= dec(limits["maximum"]):
                    raise GridError("Inventory quantity exceeds venue limits")
        if not timestamps or max(timestamps) - min(timestamps) > experiment.base.max_pair_skew_seconds:
            raise GridError("Inventory quotes are too far apart; all accounts paused")
        for plan in self.plans.values():
            if len(plan["before"]) != 64 or set(plan["target"]) != set(SYMBOLS):
                raise GridError("Invalid inventory account transition")
            for symbol in SYMBOLS:
                qty = abs(dec(plan["target"][symbol]))
                if qty % dec(self.execution_step):
                    raise GridError("Inventory target is not executable at the common quantity step")
                if qty and quote_key(symbol, qty) not in self.quotes:
                    raise GridError("Missing size-specific position exit quote")


def common_step(values):
    values = [dec(v) for v in values]
    exponent = max(max(0, -v.as_tuple().exponent) for v in values)
    if exponent > 12 or any(v <= 0 for v in values):
        raise GridError("Unsupported venue quantity precision")
    scale = 10 ** exponent
    return D(math.lcm(*(int(v * scale) for v in values))) / scale


def execution_quantum(quantity, tick, minimum):
    """Conservative simulation quantum, not a redefinition of the venue tick.

    A divisor of the base lot keeps all net trades executable and at least the
    venue minimum. Prefer a divisor of half a base lot when the venue allows it,
    so a one-lot directional difference can still be neutralized symmetrically.
    """
    quantity, tick, minimum = dec(quantity), dec(tick), dec(minimum)
    if quantity % tick or quantity < minimum:
        raise GridError("Base order quantity is incompatible with venue limits")
    count = int(quantity / tick)
    lower = max(1, int((minimum / tick).to_integral_value(rounding=ROUND_CEILING)))
    count_for_divisors = count // 2 if count % 2 == 0 and count // 2 >= lower else count
    candidates = []
    for divisor in range(1, math.isqrt(count_for_divisors) + 1):
        if count_for_divisors % divisor == 0:
            candidates.extend(d for d in (divisor, count_for_divisors // divisor) if d >= lower)
    return tick * min(candidates)


class InventoryMarketFeed:
    def __init__(self, experiment, client=None):
        self.experiment = experiment
        self.client = client or Client(experiment.base.session_file)
        self.hour = None
        self.centers = None

    def quote(self, symbol, quantity):
        data = self.client.request("POST", "/quotes/indicative", body={
            "instrument": {"underlying": symbol, "instrument_type": "perpetual_rwa_future", "settlement_asset": "USDC", "kind": "commodity"},
            "qty": str(quantity)})
        quote = Quote.parse(symbol, quantity, data)
        limits = data["qty_limits"]
        tick = common_step([limits[side]["min_qty_tick"] for side in ("bid", "ask")])
        return {**asdict(quote), "limits": {"tick": str(tick),
                "minimum": str(max(dec(limits[side]["min_qty"]) for side in ("bid", "ask"))),
                "maximum": str(min(dec(limits[side]["max_qty"]) for side in ("bid", "ask")))}}

    def next(self, cohort):
        from .cli import paired
        hour = int(time.time()) // HOUR * HOUR
        if hour != self.hour:
            rows = paired(lambda s: self.client.candles(s, hour, 72))
            center = rolling_center(*rows, hour, 72)
            cl_center = sum((dec(r["close"]) for r in rows[0] if hour - 72 * HOUR <= dec(r["unix_time_ms"]) / 1000 < hour), D(0)) / 72
            self.centers = {"CL": str(cl_center), "BZ": str(cl_center + center), "spread": str(center)}
            self.hour = hour
        markets = paired(self.client.market)
        if not all(opened for opened, _ in markets):
            raise GridError("CL or BZ market is closed; all inventory experiments paused")
        quantity = dec(self.experiment.base.quantity_barrels)
        signal_quotes = paired(lambda s: self.quote(s, quantity))
        quotes = {quote_key(s, quantity): q for s, q in zip(SYMBOLS, signal_quotes)}
        step = common_step([self.experiment.settings.execution_step_barrels, *(q["limits"]["tick"] for q in signal_quotes)])
        minimum = max(dec(q["limits"]["minimum"]) for q in signal_quotes)
        step = execution_quantum(quantity, step, minimum)
        now = time.time()
        if int(now) // HOUR * HOUR != self.hour:
            raise GridError("UTC hour changed while fetching inventory data; refresh history next poll")
        frame = cohort.prepare({s: str(q["mark"]) for s, q in zip(SYMBOLS, signal_quotes)}, self.centers,
                               now, not any(close_only for _, close_only in markets), str(step), "live_indicative")
        required = cohort.required_quotes(frame)
        missing = sorted(required - set(quotes))
        with ThreadPoolExecutor(max_workers=4) as executor:
            jobs = {key: executor.submit(self.quote, key.split(":")[0], dec(key.split(":")[1])) for key in missing}
            for key, job in jobs.items():
                quotes[key] = job.result()
        frame.quotes = quotes
        frame.ts = time.time()
        # next shadow entries were decided from the initial sample, without future marks.
        frame.validate(self.experiment)
        cohort.finalize(frame)
        return frame


class InventoryCohort(Cohort):
    def create_store(self, config):
        return InventoryStore(config.state_file, config)

    def create_engine(self, config, store):
        return InventoryEngine(config, store)

    def decode_frame(self, raw):
        return InventoryFrame.decode(raw)

    def signal_state(self):
        row = self.db.execute("SELECT payload FROM frames ORDER BY ts DESC LIMIT 1").fetchone()
        return self.decode_frame(row[0]).signals if row else empty_signals()

    def prepare(self, marks, centers, now, allow_open=True, step="0.01", data_kind="synthetic"):
        config = next(iter(self.experiment.scenarios.values()))
        signals, raw, components, _ = signal_step(self.signal_state(), marks, centers, now, config, allow_open)
        plans = {name: engine.preview(raw, step, allow_open) for name, engine in self.engines.items()}
        return InventoryFrame(now, int(now) // HOUR * HOUR, centers, marks, {}, signals, raw, components, plans,
                              allow_open, data_kind, step)

    def required_quotes(self, frame):
        required = set()
        for name, plan in frame.plans.items():
            account = self.stores[name].account()
            for symbol in SYMBOLS:
                old = dec(account["positions"][symbol]["qty"])
                new = dec(plan["target"][symbol])
                for amount in (old, new, new - old):
                    if amount:
                        required.add(quote_key(symbol, amount))
        return required

    def finalize(self, frame):
        frame.validate(self.experiment)
        if self.required_quotes(frame) - set(frame.quotes):
            raise GridError("Missing size-specific execution quote; all accounts paused")
        for name, engine in self.engines.items():
            account = self.stores[name].account()
            old_equity = engine.estimate(account, frame)[0]
            old_peak = max(dec(account["peak"]), old_equity)
            frame.plans[name]["observed_peak"] = str(old_peak)
            _, _, candidate = engine.calculate(frame, frame.plans[name])
            if ((old_peak - old_equity) / old_peak >= dec(engine.config.max_drawdown_fraction)
                    or dec(candidate["drawdown_fraction"]) >= dec(engine.config.max_drawdown_fraction)):
                frame.plans[name].update(target={s: "0" for s in SYMBOLS}, overlay="0",
                                         control_status="halted", halted="max_drawdown")
                engine.calculate(frame, frame.plans[name])  # Already collected full-close quantities.
        frame.validate(self.experiment)

    def ingest(self, frame):
        frame.validate(self.experiment)
        previous = self.latest()
        if previous is not None and previous["data_kind"] != frame.data_kind:
            raise GridError("Synthetic and live observations require separate experiment directories")
        # Verify every plan against current state before durably appending any frame.
        for name, engine in self.engines.items():
            engine.calculate(frame, frame.plans[name])
        return super().ingest(frame)

    def apply(self, frame):
        rows = []
        for name, engine in self.engines.items():
            store = self.stores[name]
            if float(store.get("last_tick", "0")) < frame.ts:
                rows.append(engine.apply(frame, frame.plans[name]))
            else:
                rows.append(store.snapshot())
            if float(store.get("last_tick")) != frame.ts:
                raise GridError("Inventory account timestamps differ; comparison not published")
        previous = self.latest()
        if previous is not None and previous["ts"] == frame.ts:
            return previous
        if previous is not None and previous["ts"] > frame.ts:
            raise GridError("Cannot publish an older inventory frame")
        base = self.experiment.base
        summary = {"mode": "inventory_comparison", "ts": frame.ts, "time_utc": utc(frame.ts),
                   "started_utc": previous["started_utc"] if previous else utc(frame.ts),
                   "sample_count": previous["sample_count"] + 1 if previous else 1,
                   "center": frame.centers["spread"], "center_window_hours": 72, "poll_seconds": base.poll_seconds,
                   "data_kind": frame.data_kind, "pnl_basis": "before_funding", "scenarios": rows,
                   "parameters": {**asdict(self.experiment.settings), "quantity_barrels": base.quantity_barrels,
                                  "paper_balance_usdc": base.paper_balance_usdc, "paper_leverage": base.paper_leverage,
                                  "fee_bps_per_leg": base.fee_bps_per_leg, "slippage_bps_per_leg": base.slippage_bps_per_leg,
                                  "max_drawdown_fraction": base.max_drawdown_fraction, "max_holding_hours": base.max_holding_hours,
                                  "effective_execution_step_barrels": frame.execution_step}}
        with self.db:
            self.db.execute("INSERT INTO summaries VALUES (?,?)", (frame.ts, encoded(summary)))
        self.set_runtime("running")
        return summary

    def report(self):
        row = self.db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
        runtime = json.loads(row[0]) if row else {"status": "starting"}
        summary = self.latest()
        public = self.experiment.output / "public"
        public.mkdir(exist_ok=True)
        write_json(public / "summary.json", {"runtime": runtime, "summary": summary})
        rows = [] if not summary else summary["scenarios"]
        body = "".join("<tr>" + "".join("<td>" + html.escape(str(r[k])) + "</td>" for k in
                       ("name", "total_pnl_usdc", "max_drawdown_fraction", "directional_barrels", "inventory_ratio", "turnover_usdc")) + "</tr>" for r in rows)
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>库存偏差模拟对照</title><h1>库存偏差模拟对照</h1><p>收益 USDC；回撤、偏离为比例；方向敞口为桶。未计资金费。完整监控使用 dashboard 命令。</p><table><tr><th>组别</th><th>总损益</th><th>最大回撤</th><th>方向敞口</th><th>偏离比例</th><th>成交额</th></tr>' + body + '</table></html>'
        temporary = public / "index.html.new"
        temporary.write_text(page, encoding="utf-8")
        temporary.replace(public / "index.html")


def run_inventory(args, experiment):
    from .cli import emit
    from .reset import process_reset, read_state
    feed = InventoryMarketFeed(experiment)
    emit({"event": "session", **feed.client.check_session()})
    with InventoryCohort(experiment) as cohort:
        stop = experiment.output / "STOP"
        stop.unlink(missing_ok=True)
        count, failures = 0, 0
        while not stop.exists():
            started = time.monotonic()
            try:
                if process_reset(cohort):
                    feed.hour = None
                frame = feed.next(cohort)
            except GridError as error:
                failures += 1
                cohort.set_runtime("paused", str(error))
                emit({"status": "paused", "reason": str(error)})
            else:
                result = cohort.ingest(frame)
                failures = 0
                emit({"mode": result["mode"], "time_utc": result["time_utc"], "sample_count": result["sample_count"],
                      "scenarios": [{k: r[k] for k in ("name", "total_pnl_usdc", "directional_barrels", "inventory_ratio", "max_drawdown_fraction", "turnover_usdc", "halted")} for r in result["scenarios"]]})
            count += 1
            if args.once or args.iterations and count >= args.iterations:
                return 2 if failures else 0
            delay = experiment.base.poll_seconds if not failures else min(60, experiment.base.poll_seconds * 2 ** min(failures, 3))
            deadline = started + delay
            while time.monotonic() < deadline and not stop.exists():
                if read_state(experiment)["status"] in {"pending", "archiving", "clearing"}:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        stop.unlink(missing_ok=True)
    return 0


def read_inventory_dashboard(experiment, window):
    from .dashboard import read_db, WINDOWS
    from .reset import read_state
    result = {"generated_utc": utc(time.time()), "runtime": {"status": "starting"}, "summary": None,
              "reset": read_state(experiment), "details_available": False, "positions": [], "trades": [],
              "history": {"window": window, "names": [], "source_count": 0, "points": []}}
    if result["reset"] and result["reset"]["status"] in {"archiving", "clearing"}:
        result["runtime"] = {"status": "resetting"}
        return result
    path = experiment.output / "comparison.sqlite3"
    if not path.is_file():
        return result
    with closing(read_db(path)) as db:
        db.execute("BEGIN")
        manifest = json.loads((experiment.output / "experiment.json").read_text(encoding="utf-8"))
        if manifest != experiment.identity():
            raise GridError("Inventory monitor configuration differs from saved experiment")
        raw = db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
        if raw:
            runtime = json.loads(raw[0])
            result["runtime"] = {k: runtime.get(k) for k in ("status", "reason", "updated_utc")}
        raw = db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()
        if raw is None:
            return result
        summary = result["summary"] = json.loads(raw[0])
        start = summary["ts"] - WINDOWS[window]
        records = db.execute("SELECT s.payload,f.payload FROM summaries s JOIN frames f ON f.ts=s.ts WHERE s.ts>=? ORDER BY s.ts", (start,)).fetchall()
        points, segment, previous = [], 0, None
        for raw_summary, raw_frame in records:
            row, frame = json.loads(raw_summary), json.loads(raw_frame)
            if previous is not None and row["ts"] - previous > max(60, experiment.base.poll_seconds * 3):
                segment += 1
            previous = row["ts"]
            points.append({"ts": row["ts"], "segment": segment,
                           "spread": str(dec(frame["marks"]["BZ"]) - dec(frame["marks"]["CL"])), "center": row["center"],
                           "scenarios": {r["name"]: {k: r[k] for k in ("total_pnl_usdc", "inventory_ratio", "directional_barrels")} for r in row["scenarios"]}})
    count = len(points)
    # Preserve per-series PnL and ratio extrema and segment edges; at most 900 points.
    if count > 900:
        names = list(experiment.scenarios)
        per_bucket = 2 + 4 * len(names)
        bucket = math.ceil(count / max(1, 900 // per_bucket))
        reduced = []
        for start in range(0, count, bucket):
            chunk = points[start:start + bucket]
            keep = {0, len(chunk) - 1}
            for name in names:
                for key in ("total_pnl_usdc", "inventory_ratio"):
                    keep.add(min(range(len(chunk)), key=lambda i: dec(chunk[i]["scenarios"][name][key])))
                    keep.add(max(range(len(chunk)), key=lambda i: dec(chunk[i]["scenarios"][name][key])))
            reduced.extend(chunk[i] for i in sorted(keep))
        points = reduced
    result["history"] = {"window": window, "names": list(experiment.scenarios), "source_count": count, "points": points}
    for row in summary["scenarios"]:
        name = row["name"]
        result["positions"].extend({"scenario": name, **p} for p in row["positions"] if dec(p["qty"]))
        with closing(read_db(experiment.scenarios[name].state_file)) as db:
            db.row_factory = sqlite3.Row
            identity = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
            if not identity or identity[0] != experiment.scenarios[name].strategy_identity():
                raise GridError("Inventory account settings changed")
            fills = db.execute("SELECT * FROM fills WHERE ts<=? ORDER BY ts DESC,id DESC LIMIT 100", (summary["ts"],)).fetchall()
            result["trades"].extend({"scenario": name, **dict(r)} for r in fills)
    result["trades"].sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
    result["details_available"] = True
    return result


def synthetic_quote(symbol, quantity, mark, ts):
    # Deterministic demo depth model; deliberately pays more for larger net orders.
    half_width = D("0.005") + dec(quantity) * D("0.001")
    return {"symbol": symbol, "qty": str(quantity), "mark": str(mark), "bid": str(dec(mark) - half_width),
            "ask": str(dec(mark) + half_width), "ts": ts}


def demo_inventory(args):
    from .cli import emit
    from .models import Config
    output = Path(args.output).resolve()
    config_dir = output.with_name(output.name + "-config")
    if output.exists() or config_dir.exists():
        raise GridError("Inventory demo requires a new output directory; existing data preserved")
    settings = InventorySettings()
    base = Config(grid_step_percent="1", max_levels=None, max_margin_fraction=None, paper_leverage="100")
    scenarios = {name: InventoryConfig(base, settings, name, limit, str(output / "ledgers" / (name + ".sqlite3")))
                 for name, limit in (("delta-0pct", "0"), ("delta-5pct", "5"), ("delta-10pct", "10"), ("delta-20pct", "20"), ("delta-uncontrolled", None))}
    experiment = InventoryExperiment(base, output, scenarios, settings)
    base_time = 1735689600
    with InventoryCohort(experiment) as cohort:
        for i in range(361):
            if args.trajectory == "trend":
                common = -D(i) * D("0.035")
                spread = D("4") + D(str(math.sin(i / 17))) * D("0.10")
            elif args.trajectory == "divergence":
                common = D(str(math.sin(i / 31))) * D("0.30")
                spread = D("4") + D(i) * D("0.015")
            else:
                common = D(str(math.sin(i / 21))) * D("1.8")
                spread = D("4") + D(str(math.sin(i / 37))) * D("0.45")
            marks = {"CL": str(D("100") + common - (spread - 4) / 2), "BZ": str(D("104") + common + (spread - 4) / 2)}
            now = base_time + i * 10
            frame = cohort.prepare(marks, {"CL": "100", "BZ": "104", "spread": "4"}, now)
            needed = cohort.required_quotes(frame) | {quote_key(s, 1) for s in SYMBOLS}
            frame.quotes = {key: synthetic_quote(key.split(":")[0], dec(key.split(":")[1]), marks[key.split(":")[0]], now) for key in needed}
            cohort.finalize(frame)
            result = cohort.ingest(frame)
        # Save portable config for the normal dashboard and status commands.
        (output / "base.json").write_text(json.dumps(asdict(base), indent=2) + "\n", encoding="utf-8")
        demo_config = {"kind": "inventory", "base_config": "base.json", "output_dir": ".", "strategy": asdict(settings),
                       "scenarios": [{"name": n, "tolerance_percent": c.tolerance_percent} for n, c in scenarios.items()]}
        # Config cannot live inside ledger output: keep it in an adjacent sibling directory.
        config_dir.mkdir()
        (output / "base.json").replace(config_dir / "base.json")
        demo_config["output_dir"] = "../" + output.name
        (config_dir / "inventory.json").write_text(json.dumps(demo_config, indent=2) + "\n", encoding="utf-8")
        emit({"demo": "synthetic_scenario_not_backtest", "trajectory": args.trajectory,
              "experiments": str(config_dir / "inventory.json"), "summary": result})
