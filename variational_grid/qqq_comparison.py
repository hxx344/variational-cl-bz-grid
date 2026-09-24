"""Journaled nine-account QQQ / US100 paper experiment and public-feed runner."""
from contextlib import closing, nullcontext
from dataclasses import asdict, dataclass
import base64
import html
import json
import math
import os
from pathlib import Path
import re
import time
import zlib
from types import SimpleNamespace

from .comparison import Cohort, write_json
from .models import D, GridError, dec, utc
from .qqq_hedge import QQQConfig, QQQEngine, QQQSettings, QQQStore, digest, encoded, hedge_target
from .qqq_market import RequestDeferred


def summary_record(summary):
    return encoded({"qqq_compact": 1, "history": {"pnl": [r["total_pnl_usdc"] for r in summary["scenarios"]],
                    "exposure": [r["signed_exposure_percent"] for r in summary["scenarios"]], "gap": summary["market"].get("gap", False)},
                    "state": base64.b64encode(zlib.compress(encoded(summary).encode(), 3)).decode("ascii")})


def decode_summary(raw):
    data = json.loads(raw)
    return json.loads(zlib.decompress(base64.b64decode(data["state"]))) if data.get("qqq_compact") == 1 else data


def write_export(path, text):
    """Windows scanners/readers can briefly hold a derived report during replace."""
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(.02 * (attempt + 1))


@dataclass
class QQQExperiment:
    base: object
    output: Path
    scenarios: dict
    settings: QQQSettings
    kind = "qqq_hedge"

    @classmethod
    def load(cls, path):
        from .cli import configuration
        path = Path(path).resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if set(data) != {"kind", "base_config", "output_dir", "strategy", "scenarios"} or data["kind"] != cls.kind:
                raise ValueError()
            base_path = (path.parent / data["base_config"]).resolve()
            old = configuration(base_path)
            settings = QQQSettings(**data["strategy"]).validate()
            base = SimpleNamespace(session_file=old.session_file, poll_seconds=settings.poll_seconds)
            output = (path.parent / data["output_dir"]).resolve()
            if any(p.is_relative_to(output) for p in (path, base_path, Path(old.session_file), Path(old.state_file))):
                raise GridError("QQQ output must be separate from existing configuration, session and ledger")
            if not isinstance(data["scenarios"], list) or not 1 <= len(data["scenarios"]) <= 20:
                raise ValueError()
            scenarios, combinations = {}, set()
            for item in data["scenarios"]:
                if set(item) != {"name", "grid_step_percent", "hedge_tolerance_percent"}:
                    raise ValueError()
                name = item["name"]
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,39}", name) or name.lower() in {n.lower() for n in scenarios}:
                    raise ValueError()
                step, tolerance = dec(item["grid_step_percent"]), dec(item["hedge_tolerance_percent"])
                if not 0 < step * settings.grid_count < 100 or not 0 <= tolerance < 100 or (step, tolerance) in combinations:
                    raise ValueError()
                combinations.add((step, tolerance))
                scenarios[name] = QQQConfig(settings, name, str(step), str(tolerance), str(output / "ledgers" / (name + ".sqlite3")))
            return cls(base, output, scenarios, settings)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise GridError("Invalid QQQ hedge experiment; require separate output and unique spacing/tolerance scenarios") from None

    def identity(self):
        return {"kind": self.kind, "version": 1,
                "scenarios": {name: json.loads(c.strategy_identity()) for name, c in self.scenarios.items()}}


@dataclass
class QQQFrame:
    ts: float
    market: dict
    quotes: dict
    plans: dict
    data_kind: str = "live_indicative"

    def encode(self):
        return b"QQQ1" + zlib.compress(encoded(asdict(self)).encode(), 3)

    @classmethod
    def decode(cls, raw):
        if isinstance(raw, bytes) and raw.startswith(b"QQQ1"):
            raw = zlib.decompress(raw[4:])
        return cls(**json.loads(raw))

    def validate(self, experiment):
        settings = experiment.settings
        if not math.isfinite(self.ts) or self.ts <= 0 or self.data_kind not in {"synthetic", "live_indicative"} or set(self.plans) != set(experiment.scenarios):
            raise GridError("Invalid QQQ shared observation")
        q = self.market["lighter"]
        if type(q["gap"]) is not bool or type(q["ready"]) is not bool or type(self.market["allow_entries"]) is not bool:
            raise GridError("Invalid QQQ feed status")
        for value in (q["price_tick"], q["size_step"], q["min_qty"]):
            if dec(value) <= 0:
                raise GridError("Invalid QQQ market increments")
        if dec(q["bid"]) <= 0 or dec(q["ask"]) < dec(q["bid"]) or dec(q["mark"]) <= 0:
            raise GridError("Invalid QQQ book")
        if not math.isfinite(q["ts"]) or self.ts - q["ts"] < -2:
            raise GridError("Invalid QQQ book timestamp")
        if self.ts - q["ts"] > settings.max_quote_age_seconds and (self.market["allow_entries"] or self.market["var"]):
            raise GridError("Stale QQQ book cannot support new orders or hedge valuation")
        ids, last_ts = set(), 0
        for trade in q["trades"]:
            if trade["id"] in ids or trade["side"] not in {"buy", "sell"} or dec(trade["qty"]) <= 0 or dec(trade["price"]) <= 0:
                raise GridError("Invalid QQQ trade flow")
            if not math.isfinite(trade["ts"]) or trade["ts"] < last_ts or trade["ts"] > self.ts + 2:
                raise GridError("Invalid QQQ trade timestamp")
            ids.add(trade["id"])
            last_ts = trade["ts"]
        for side in ("bids", "asks"):
            for p, qty in q[side]:
                if dec(p) <= 0 or dec(qty) < 0:
                    raise GridError("Invalid QQQ depth")
        var = self.market["var"]
        if var and not q["ready"]:
            raise GridError("Unavailable QQQ source cannot support hedge valuation")
        for quote in ([var] if var else []) + list(self.quotes.values()):
            if dec(quote["bid"]) <= 0 or dec(quote["ask"]) < dec(quote["bid"]) or dec(quote["mark"]) <= 0 or dec(quote["qty"]) <= 0:
                raise GridError("Invalid Var indicative quote")
            if not math.isfinite(quote["ts"]) or not -2 <= self.ts - quote["ts"] <= settings.max_quote_age_seconds:
                raise GridError("Var indicative quote is stale")
            if abs(q["ts"] - quote["ts"]) > settings.max_pair_skew_seconds:
                raise GridError("QQQ and US100 observations are too far apart")
            if dec(quote["size_step"]) <= 0 or dec(quote["qty"]) % dec(quote["size_step"]):
                raise GridError("Invalid Var quantity increment")
        for key, quote in self.quotes.items():
            if key != format(dec(quote["qty"]).normalize(), "f") or not dec(quote["min_qty"]) <= dec(quote["qty"]) <= dec(quote["max_qty"]):
                raise GridError("Var quantity-specific quote mismatch")
        if self.market["allow_entries"] and (not var or not q["ready"] or q["gap"] or q.get("close_only", False) or var.get("close_only", False)):
            raise GridError("Cannot open grid entries with incomplete market data")
        for plan in self.plans.values():
            if not isinstance(plan["before"], str) or len(plan["before"]) != 64:
                raise GridError("Invalid QQQ plan state hash")
            dec(plan["target"])


class QQQCohort(Cohort):
    def latest(self):
        row = self.db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()
        return decode_summary(row[0]) if row else None

    def create_store(self, config):
        return QQQStore(config.state_file, config)

    def create_engine(self, config, store):
        return QQQEngine(config, store)

    def decode_frame(self, raw):
        return QQQFrame.decode(raw)

    def prepare_frame(self, ts, market, quotes=None, data_kind="live_indicative"):
        plans = {}
        for name, engine in self.engines.items():
            account, _ = engine.prepare(market, ts)
            var = market["var"]
            target = hedge_target(account, market["lighter"]["mark"], var["mark"], engine.config, var["size_step"]) if var else dec(account["us100"]["qty"])
            plans[name] = {"before": digest(self.stores[name].account()), "target": str(target)}
        return QQQFrame(ts, market, quotes or {}, plans, data_kind)

    def ingest(self, frame):
        frame.validate(self.experiment)
        previous = self.latest()
        if previous and previous["data_kind"] != frame.data_kind:
            raise GridError("Synthetic and public-feed simulations require separate output directories")
        for name, engine in self.engines.items():
            engine.calculate(frame, frame.plans[name])
        return super().ingest(frame)

    def apply(self, frame):
        rows = []
        for name, engine in self.engines.items():
            store = self.stores[name]
            rows.append(engine.apply(frame, frame.plans[name]) if float(store.get("last_tick", "0")) < frame.ts else store.snapshot())
            if float(store.get("last_tick")) != frame.ts:
                raise GridError("QQQ account timestamps differ")
        previous = self.latest()
        if previous and previous["ts"] == frame.ts:
            return previous
        if previous and previous["ts"] > frame.ts:
            raise GridError("Cannot publish an older QQQ observation")
        q, v = frame.market["lighter"], frame.market["var"]
        summary = {"kind": "qqq_hedge", "mode": "qqq_hedge_comparison", "ts": frame.ts, "time_utc": utc(frame.ts),
                   "started_utc": previous["started_utc"] if previous else utc(frame.ts), "sample_count": previous["sample_count"] + 1 if previous else 1,
                   "poll_seconds": self.experiment.settings.poll_seconds, "data_kind": frame.data_kind,
                   "pnl_basis": "before_funding_and_dividends", "parameters": asdict(self.experiment.settings), "scenarios": rows,
                   "market": {"qqq_bid": q["bid"], "qqq_ask": q["ask"], "qqq_mark": q["mark"],
                              "qqq_mark_source": q.get("mark_source", "book_mid"),
                              "us100_bid": v["bid"] if v else None, "us100_ask": v["ask"] if v else None, "us100_mark": v["mark"] if v else None,
                              "qqq_source_ts": q.get("source_ts", q["ts"]), "qqq_source_time_kind": q.get("source_time_kind", "observed"),
                              "var_source_ts": v["ts"] if v else None,
                              "source_status": "ready" if frame.market["allow_entries"] else "paused_entries",
                              "source_reason": frame.market.get("reason", ""), "gap": q["gap"]}}
        with self.db:
            self.db.execute("INSERT INTO summaries VALUES (?,?)", (frame.ts, summary_record(summary)))
        self.set_runtime("running" if frame.market["allow_entries"] else "degraded", frame.market.get("reason") or None)
        return summary

    def report(self):
        raw = self.db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
        runtime = json.loads(raw[0]) if raw else {"status": "starting"}
        summary = self.latest()
        public = self.experiment.output / "public"
        public.mkdir(exist_ok=True)
        write_export(public / "summary.json", encoded({"runtime": runtime, "summary": summary}) + "\n")
        body = "".join("<tr>" + "".join("<td>" + html.escape(str(row[k])) + "</td>" for k in
                                       ("name", "total_pnl_usdc", "turnover_usdc", "exposure_percent")) + "</tr>" for row in (summary["scenarios"] if summary else []))
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>QQQ / US100 模拟</title><h1>QQQ / US100 模拟</h1><p>损益、成交额：USDC；敞口：%。未计资金费、隔夜费和股息调整。完整监控使用 dashboard 命令。</p><table><tr><th>账户</th><th>损益</th><th>成交额</th><th>敞口</th></tr>' + body + '</table></html>'
        write_export(public / "index.html", page)


class QQQMarketFeed:
    def __init__(self, experiment, lighter=None, var=None):
        from .qqq_market import LighterClient, VarSwapClient
        self.experiment = experiment
        self.lighter = lighter or LighterClient()
        self.var = var or VarSwapClient()
        self._quote_cursor = 0
        self._cooldown_path = experiment.output / "market-cooldowns.json"
        self._saved_cooldowns = None
        if self._cooldown_path.exists():
            try:
                saved = json.loads(self._cooldown_path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError()
                for client in (self.lighter, self.var):
                    transport = getattr(client, "transport", None)
                    if transport and transport.venue in saved:
                        transport.restore(saved[transport.venue])
            except (OSError, ValueError, TypeError):
                raise GridError("Cannot read saved market cooldowns") from None

    def save_cooldowns(self):
        state = {client.transport.venue: client.transport.state() for client in (self.lighter, self.var)
                 if getattr(client, "transport", None)}
        text = encoded(state) + "\n"
        if state and text != self._saved_cooldowns:
            write_export(self._cooldown_path, text)
            self._saved_cooldowns = text

    def next(self, cohort):
        try:
            transport = getattr(self.var, "transport", None)
            with transport.quote_batch() if transport else nullcontext():
                return self._next(cohort)
        finally:
            # Persist even when Lighter fails before a frame can be published.
            # The ledger reset deliberately leaves this transport state intact.
            self.save_cooldowns()

    def _next(self, cohort):
        q = self.lighter.snapshot()
        reason, var = q.get("reason", ""), None
        try:
            var = self.var.market()
            if not var.get("market_open", True):
                reason, var = "US100 market closed; maker entries paused", None
        except GridError as error:
            reason = str(error)
        now = time.time()
        settings = self.experiment.settings
        if not q["ready"]:
            var = None
        if var and (now - var["ts"] > settings.max_quote_age_seconds or abs(q["ts"] - var["ts"]) > settings.max_pair_skew_seconds):
            reason, var = "US100 quote stale; maker entries paused", None
        if now - q["ts"] > settings.max_quote_age_seconds:
            reason, var = "QQQ observation delayed; account known fills and defer new decisions", None
        if q.get("close_only", False) or (var or {}).get("close_only", False):
            reason = "Venue in close-only mode; maker entries paused"
        market = {"lighter": q, "var": var, "allow_entries": bool(var and q["ready"] and not q["gap"] and not q.get("close_only", False) and not var.get("close_only", False)), "reason": reason}
        frame = cohort.prepare_frame(now, market)
        quantities = {}
        names = list(frame.plans)
        ordered = names[self._quote_cursor:] + names[:self._quote_cursor]
        if var:
            for name in ordered:
                plan = frame.plans[name]
                change = abs(dec(plan["target"]) - dec(cohort.stores[name].account()["us100"]["qty"]))
                if change and dec(var["min_qty"]) <= change <= dec(var["max_qty"]):
                    quantities.setdefault(format(change.normalize(), "f"), name)
        collected = {}
        for qty, name in quantities.items():
            try:
                collected[qty] = self.var.quote(dec(qty))
            except RequestDeferred as error:
                market["reason"] = str(error)
                if error.limited:
                    market.update(var=None, allow_entries=False)
                    self._quote_cursor = (names.index(name) + 1) % len(names)
                break  # Do not burst the remaining quantities into a cooling venue.
            except GridError as error:
                market.update(reason=str(error), allow_entries=False)
                self._quote_cursor = (names.index(name) + 1) % len(names)
                break
            self._quote_cursor = (names.index(name) + 1) % len(names)
        # Network latency is observable. Rebuild targets at the final observation time;
        # use only quotes actually fetched for the resulting exact signed change.
        now = time.time()
        if now - q["ts"] > settings.max_quote_age_seconds or (var and (now - var["ts"] > settings.max_quote_age_seconds or abs(q["ts"] - var["ts"]) > settings.max_pair_skew_seconds)):
            market.update(var=None, allow_entries=False, reason="US100 quote expired while gathering hedge prices")
        frame = cohort.prepare_frame(now, market)
        if market["var"]:
            for key, quote in collected.items():
                if quote and -2 <= now - quote["ts"] <= settings.max_quote_age_seconds and abs(q["ts"] - quote["ts"]) <= settings.max_pair_skew_seconds:
                    frame.quotes[key] = quote
        frame.validate(self.experiment)
        return frame


def run_qqq(args, experiment):
    from .cli import emit
    from .reset import process_reset, read_state
    feed = QQQMarketFeed(experiment)
    with QQQCohort(experiment) as cohort:
        stop = experiment.output / "STOP"
        stop.unlink(missing_ok=True)
        count, failures = 0, 0
        while not stop.exists():
            started = time.monotonic()
            try:
                process_reset(cohort)  # Reset accounts without bypassing venue cooldowns.
                frame = feed.next(cohort)
            except GridError as error:
                failures += 1
                cohort.set_runtime("paused", str(error))
                emit({"status": "paused", "reason": str(error)})
            else:
                result = cohort.ingest(frame)
                failures = 0
                emit({"mode": result["mode"], "time_utc": result["time_utc"], "sample_count": result["sample_count"],
                      "scenarios": [{k: row[k] for k in ("name", "total_pnl_usdc", "turnover_usdc", "exposure_percent", "hedge_pending")} for row in result["scenarios"]]})
            count += 1
            if args.once or args.iterations and count >= args.iterations:
                return 2 if failures else 0
            deadline = started + (min(60, experiment.settings.poll_seconds * 2 ** min(failures, 3)) if failures else experiment.settings.poll_seconds)
            while time.monotonic() < deadline and not stop.exists():
                if read_state(experiment)["status"] in {"pending", "archiving", "clearing"}:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        stop.unlink(missing_ok=True)
    return 0


def read_qqq_dashboard(experiment, window):
    from .dashboard import read_db, WINDOWS
    from .reset import read_state
    result = {"kind": "qqq_hedge", "server_ts": time.time(), "runtime": {"status": "starting"}, "summary": None,
              "reset": read_state(experiment), "details_available": False, "positions": [], "trades": [],
              "history": {"range": window, "names": [], "source_count": 0, "points": []}}
    try:
        saved = json.loads((experiment.output / "market-cooldowns.json").read_text(encoding="utf-8"))
        result["rate_limits"] = [{"venue": venue, "retry_at": state["retry_at"]} for venue, state in saved.items()
                                 if venue in {"Lighter", "Variational"} and isinstance(state, dict)
                                 and type(state.get("retry_at")) in (int, float) and math.isfinite(state["retry_at"])
                                 and state["retry_at"] > 0]
    except (OSError, ValueError, AttributeError):
        result["rate_limits"] = []
    if result["reset"] and result["reset"]["status"] in {"archiving", "clearing"}:
        result["runtime"] = {"status": "resetting"}
        return result
    path = experiment.output / "comparison.sqlite3"
    if not path.is_file():
        return result
    if json.loads((experiment.output / "experiment.json").read_text(encoding="utf-8")) != experiment.identity():
        raise GridError("QQQ dashboard settings differ from saved experiment")
    with closing(read_db(path)) as db:
        db.execute("BEGIN")
        raw = db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
        if raw:
            runtime = json.loads(raw[0])
            result["runtime"] = {k: runtime.get(k) for k in ("status", "reason", "updated_utc")}
        raw = db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()
        if not raw:
            return result
        summary = result["summary"] = decode_summary(raw[0])
        names = [r["name"] for r in summary["scenarios"]]
        points, segment, previous = [], 0, None
        rows = db.execute("SELECT ts,payload FROM summaries WHERE ts>=? AND ts<=? ORDER BY ts", (summary["ts"] - WINDOWS[window], summary["ts"]))
        for ts, raw in rows:
            data = json.loads(raw)
            history = data["history"] if data.get("qqq_compact") == 1 else {
                "gap": data["market"].get("gap", False), "pnl": [r["total_pnl_usdc"] for r in data["scenarios"]],
                "exposure": [r["signed_exposure_percent"] for r in data["scenarios"]]}
            if previous is not None and (ts - previous > max(15, experiment.settings.poll_seconds * 3) or history["gap"]):
                segment += 1
            points.append({"ts": ts, "segment": segment, "pnl": [float(v) for v in history["pnl"]],
                           "exposure": [float(v) for v in history["exposure"]]})
            previous = ts
        source_count = len(points)
        if source_count > 900:
            # Keep endpoints and extrema of every PnL/exposure series in each bucket.
            selected = {0, source_count - 1}
            buckets = max(1, 898 // (4 * len(names) + 2))
            for bucket in range(buckets):
                lo, hi = bucket * source_count // buckets, (bucket + 1) * source_count // buckets
                selected.update((lo, hi - 1))
                for key in ("pnl", "exposure"):
                    for j in range(len(names)):
                        selected.add(min(range(lo, hi), key=lambda i: points[i][key][j]))
                        selected.add(max(range(lo, hi), key=lambda i: points[i][key][j]))
            points = [points[i] for i in sorted(selected)]
        result["history"] = {"range": window, "names": names, "source_count": source_count, "points": points}
    for name, config in experiment.scenarios.items():
        with closing(read_db(config.state_file)) as db:
            db.execute("BEGIN")
            raw = db.execute("SELECT account FROM ticks WHERE ts=?", (summary["ts"],)).fetchone()
            if not raw:
                raise GridError("QQQ dashboard missing published account snapshot")
            account = json.loads(raw[0])
            result["positions"].extend({"scenario": name, **slot} for slot in account["slots"] if dec(slot["qty"]) > 0)
            for raw, in db.execute("SELECT payload FROM fills WHERE frame_ts<=? ORDER BY id DESC LIMIT 100", (summary["ts"],)):
                result["trades"].append({"scenario": name, **json.loads(raw)})
    result["trades"].sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
    result["details_available"] = True
    return result


def demo_qqq(args):
    from .cli import emit
    output = Path(args.output).resolve()
    if output.exists() or output.with_suffix(".json").exists():
        raise GridError("QQQ demo requires a new output directory and configuration path")
    template = Path(__file__).resolve().parent.parent / "qqq-hedge.example.json"
    data = json.loads(template.read_text(encoding="utf-8"))
    data.update(base_config=str(template.parent / "config.example.json"), output_dir=str(output))
    path = output.with_suffix(".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)
    experiment = QQQExperiment.load(path)
    start = time.time() - 1800
    with QQQCohort(experiment) as cohort:
        for i in range(361):
            ts = start + i * 5
            mark = D(str(round(740 + math.sin(i / 14) * 7 + math.sin(i / 4) * .6, 2)))
            vmark = D(str(round(30000 + (float(mark) - 740) * 40 + math.sin(i / 29) * 65, 2)))
            q = {"ts": ts, "bid": str(mark - D(".01")), "ask": str(mark + D(".01")), "mark": str(mark),
                 "price_tick": ".01", "size_step": ".0001", "min_qty": ".0075", "min_notional": "10",
                 "bids": [[str(mark - 20), "1"], [str(mark - D(".01")), "1"]],
                 "asks": [[str(mark + D(".01")), "1"], [str(mark + 20), "1"]], "gap": False, "ready": True,
                 "trades": [] if i == 0 else [{"id": str(i * 2), "ts": ts - 1, "side": "sell", "price": str(mark), "qty": "20"},
                                              {"id": str(i * 2 + 1), "ts": ts, "side": "buy", "price": str(mark), "qty": "20"}]}
            def quote(qty):
                return {"ts": ts, "qty": str(qty), "bid": str(vmark - D(".1")), "ask": str(vmark + D(".1")), "mark": str(vmark),
                        "size_step": ".000001", "min_qty": ".000004", "max_qty": "10000", "market_open": True}
            market = {"lighter": q, "var": quote(".01"), "allow_entries": True, "reason": "Synthetic demonstration; not a historical backtest"}
            frame = cohort.prepare_frame(ts, market, data_kind="synthetic")
            for name, plan in frame.plans.items():
                amount = abs(dec(plan["target"]) - dec(cohort.stores[name].account()["us100"]["qty"]))
                if amount >= D(".000004"):
                    frame.quotes[format(amount.normalize(), "f")] = quote(amount)
            cohort.ingest(frame)
        emit({"demo": "synthetic_not_backtest", "experiments": str(path), "accounts": len(experiment.scenarios),
              "scenarios": [{k: r[k] for k in ("name", "total_pnl_usdc", "turnover_usdc")} for r in cohort.latest()["scenarios"]]})
