"""Journaled QQQ / US100 paper comparison with read-only market feeds."""
from contextlib import closing, nullcontext
from dataclasses import asdict, dataclass, field
import base64
import html
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import zlib
from types import SimpleNamespace

from .comparison import Cohort, write_json
from .models import D, GridError, dec, utc
from .qqq_hedge import QQQConfig, QQQEngine, QQQSettings, QQQStore, digest, encoded, hedge_target
from .qqq_market import MarketClosed, RequestDeferred
from .qqq_pricing import QQQPricing, ReferenceCache, source_valid, reference_price
from .qqq_auth import SessionUnavailable
from .qqq_scalper import SAFETY_POLICY, ScalperSettings
from .qqq_pause import PAIR_PAUSE_POLICY, VENUE_STATES, close_times


def summary_record(summary):
    return encoded({"qqq_compact": 1, "history": {"names": [r["name"] for r in summary["scenarios"]],
                    "pnl": [r["total_pnl_usdc"] for r in summary["scenarios"]],
                    "exposure": [r["signed_exposure_percent"] for r in summary["scenarios"]],
                    "net_exposure": [r["net_exposure_usdc"] for r in summary["scenarios"]], "gap": summary["market"].get("gap", False)},
                    "state": base64.b64encode(zlib.compress(encoded(summary).encode(), 3)).decode("ascii")})


def decode_summary(raw):
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid summary object")
        if data.get("qqq_compact") == 1:
            data = json.loads(zlib.decompress(base64.b64decode(data["state"])))
        if not isinstance(data, dict):
            raise ValueError("Invalid summary state")
        return data
    except (ValueError, TypeError, KeyError, zlib.error):
        raise GridError("Invalid QQQ saved summary") from None


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
    pricing: QQQPricing = field(default_factory=QQQPricing)
    previous_output: Path | None = None
    scalper: ScalperSettings | None = None
    kind = "qqq_hedge"

    @classmethod
    def load(cls, path):
        from .cli import configuration
        path = Path(path).resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            required = {"kind", "base_config", "output_dir", "strategy", "scenarios"}
            if not required <= set(data) <= required | {"pricing", "previous_output_dir", "scalper"} or data["kind"] != cls.kind:
                raise ValueError()
            base_path = (path.parent / data["base_config"]).resolve()
            old = configuration(base_path)
            settings = QQQSettings(**data["strategy"]).validate()
            pricing = QQQPricing.load(data.get("pricing", {}))
            scalper = ScalperSettings(**data["scalper"]).validate() if "scalper" in data else None
            base = SimpleNamespace(session_file=old.session_file, poll_seconds=settings.poll_seconds)
            output = (path.parent / data["output_dir"]).resolve()
            if any(p.is_relative_to(output) for p in (path, base_path, Path(old.session_file), Path(old.state_file))):
                raise GridError("QQQ output must be separate from existing configuration, session and ledger")
            if not isinstance(data["scenarios"], list) or not 1 <= len(data["scenarios"]) <= 20:
                raise ValueError()
            scenarios, combinations = {}, set()
            for item in data["scenarios"]:
                dollars = "hedge_threshold_usdc" in item
                keys = {"name", "grid_step_percent", "hedge_threshold_usdc" if dollars else "hedge_tolerance_percent"}
                if not keys <= set(item) <= keys | ({"take_profit_percent"} if scalper else set()):
                    raise ValueError()
                name = item["name"]
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,39}", name) or name.lower() in {n.lower() for n in scenarios}:
                    raise ValueError()
                step, tolerance = dec(item["grid_step_percent"]), dec(item["hedge_threshold_usdc"] if dollars else item["hedge_tolerance_percent"])
                if not 0 < step * settings.grid_count < 100 or not (tolerance > 0 if dollars else 0 <= tolerance < 100) or (step, dollars, tolerance) in combinations:
                    raise ValueError()
                combinations.add((step, dollars, tolerance))
                profit = dec(item.get("take_profit_percent", step)) if scalper else None
                if profit is not None and not 0 < profit < 100:
                    raise ValueError()
                scenarios[name] = QQQConfig(settings, name, str(step), None if dollars else str(tolerance), str(output / "ledgers" / (name + ".sqlite3")), str(tolerance) if dollars else None, pricing.half_spread_percent, scalper, str(profit) if profit is not None else None)
            if len({c.hedge_threshold_usdc is not None for c in scenarios.values()}) != 1:
                raise GridError("QQQ scenarios must use the same hedge threshold unit")
            previous_output = (path.parent / data["previous_output_dir"]).resolve() if data.get("previous_output_dir") else None
            if previous_output == output:
                raise GridError("Previous QQQ output must be different from the new output")
            return cls(base, output, scenarios, settings, pricing, previous_output, scalper)
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
        policy = QQQPricing.load(self.market["pricing_policy"]) if "pricing_policy" in self.market else None
        if policy and policy.mode != "shared_indicative_v1":
            raise GridError("Invalid shared reference frame policy")
        if policy and any(c.half_spread_percent != policy.half_spread_percent for c in experiment.scenarios.values()):
            raise GridError("Paper half spread differs from experiment economics")
        if not math.isfinite(self.ts) or self.ts <= 0 or self.data_kind not in {"synthetic", "live_indicative"} or set(self.plans) != set(experiment.scenarios):
            raise GridError("Invalid QQQ shared observation")
        q = self.market["lighter"]
        if "pair_pause_policy" in self.market and self.market["pair_pause_policy"] != PAIR_PAUSE_POLICY:
            raise GridError("Unknown QQQ pair pause policy")
        if "var_market_state" in self.market and self.market["var_market_state"] not in VENUE_STATES:
            raise GridError("Invalid QQQ pair venue state")
        if "pair_pause_policy" in self.market:
            close_times(self.market)
        if "scalper_safety_policy" in q and q["scalper_safety_policy"] != SAFETY_POLICY:
            raise GridError("Unknown QQQ scalper safety policy")
        if "take_profit_policy" in q:
            from .qqq_execution import TAKE_PROFIT_POLICY
            if q["take_profit_policy"] != TAKE_PROFIT_POLICY:
                raise GridError("Unknown QQQ take-profit execution policy")
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
        if var and policy and (not source_valid(var, var) or self.ts >= (var.get("closes_at") or 0)
                              or not -2 <= self.ts - var["metadata_ts"] <= 120 or not var["market_open"]):
            raise GridError("Invalid or closed US100 reference market")
        if var and policy:
            original = {**var, "bid": var.get("source_bid"), "ask": var.get("source_ask")}
            if not source_valid(original, original):
                raise GridError("Invalid original reference bid/ask")
            expected = reference_price(original, policy)
            if dec(var["bid"]) != dec(expected["bid"]) or dec(var["ask"]) != dec(expected["ask"]):
                raise GridError("Paper reference spread differs from frame policy")
        for quote in ([var] if var else []) + list(self.quotes.values()):
            if dec(quote["bid"]) <= 0 or dec(quote["ask"]) < dec(quote["bid"]) or dec(quote["mark"]) <= 0 or dec(quote["qty"]) <= 0:
                raise GridError("Invalid Var indicative quote")
            if not math.isfinite(quote["ts"]) or not -2 <= self.ts - quote["ts"] <= (policy.max_age_seconds if policy else settings.max_quote_age_seconds):
                raise GridError("Var indicative quote is stale")
            if not policy and abs(q["ts"] - quote["ts"]) > settings.max_pair_skew_seconds:
                raise GridError("QQQ and US100 observations are too far apart")
            if dec(quote["size_step"]) <= 0 or dec(quote["qty"]) % dec(quote["size_step"]):
                raise GridError("Invalid Var quantity increment")
        for key, quote in self.quotes.items():
            if key != format(dec(quote["qty"]).normalize(), "f") or not dec(quote["min_qty"]) <= dec(quote["qty"]) <= dec(quote["max_qty"]):
                raise GridError("Var quantity-specific quote mismatch")
            if policy:
                if not var or quote.get("pricing_mode") != policy.mode or dec(quote.get("source_qty", "0")) != dec(var["qty"]):
                    raise GridError("Invalid shared reference quantity provenance")
                for field_name in ("bid", "ask", "mark", "ts", "source_ts", "received_ts", "min_qty", "max_qty", "size_step", "close_only", "source_bid", "source_ask"):
                    if quote.get(field_name) != var.get(field_name):
                        raise GridError("Paper estimate differs from shared source price")
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
        market = {**market, "pair_pause_policy": PAIR_PAUSE_POLICY}
        if self.experiment.scalper is not None and self.experiment.scalper.gtt_take_profit:
            from .qqq_execution import TAKE_PROFIT_POLICY
            # Version observations, not the economic identity: old pending frames
            # must recover identically across accounts before the new policy starts.
            market = {**market, "lighter": {**market["lighter"], "take_profit_policy": TAKE_PROFIT_POLICY,
                                          "scalper_safety_policy": SAFETY_POLICY}}
        plans = {}
        for name, engine in self.engines.items():
            account, _ = engine.prepare(market, ts)
            var = market["var"]
            target = hedge_target(account, market["lighter"]["mark"], var["mark"], engine.config, var["size_step"]) if var and not account.get("pair_pause", {}).get("active") else dec(account["us100"]["qty"])
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
        paused = next((r["pair_pause"] for r in rows if r.get("pair_pause", {}).get("active")), None)
        summary = {"kind": "qqq_hedge", "mode": "qqq_hedge_comparison", "ts": frame.ts, "time_utc": utc(frame.ts),
                   "started_utc": previous["started_utc"] if previous else utc(frame.ts), "sample_count": previous["sample_count"] + 1 if previous else 1,
                   "poll_seconds": self.experiment.settings.poll_seconds, "data_kind": frame.data_kind,
                   "pnl_basis": "before_funding_and_dividends", "parameters": asdict(self.experiment.settings), "scenarios": rows,
                   "market": {"qqq_bid": q["bid"], "qqq_ask": q["ask"], "qqq_mark": q["mark"],
                              "qqq_mark_source": q.get("mark_source", "book_mid"),
                              "us100_bid": v["bid"] if v else None, "us100_ask": v["ask"] if v else None, "us100_mark": v["mark"] if v else None,
                              "qqq_source_ts": q.get("source_ts", q["ts"]), "qqq_source_time_kind": q.get("source_time_kind", "observed"),
                              "var_source_ts": v["ts"] if v else None,
                              "var_source": v.get("source") if v else None,
                              "source_status": "pair_paused" if paused else "ready" if frame.market["allow_entries"] else "paused_entries",
                              "source_reason": paused["reason"] if paused else frame.market.get("reason", ""), "gap": q["gap"]}}
        if "pricing_policy" in frame.market:
            previous_start = previous.get("pricing_since_utc") if previous and previous.get("pricing") == frame.market["pricing_policy"] else None
            summary.update(pricing=frame.market["pricing_policy"], pricing_since_utc=previous_start or utc(frame.ts))
            summary["market"]["quote_cache"] = frame.market["quote_cache"]
        if self.experiment.scalper is not None:
            summary["scalper"] = asdict(self.experiment.scalper)
        with self.db:
            self.db.execute("INSERT INTO summaries VALUES (?,?)", (frame.ts, summary_record(summary)))
        self.set_runtime("paused" if paused else "running" if frame.market["allow_entries"] else "degraded",
                         paused["reason"] if paused else frame.market.get("reason") or None)
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
        self.var = var or VarSwapClient(session_file=experiment.base.session_file,
            max_age_seconds=experiment.pricing.max_age_seconds if experiment.pricing.mode == "shared_indicative_v1" else 10)
        self._quote_cursor = 0
        self._cooldown_path = experiment.output / "market-cooldowns.json"
        self._saved_cooldowns = None
        cooldown_source = self._cooldown_path
        if not cooldown_source.exists() and experiment.previous_output:
            cooldown_source = experiment.previous_output / "market-cooldowns.json"
        if cooldown_source.exists():
            try:
                saved = json.loads(cooldown_source.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError()
                for client in (self.lighter, self.var):
                    transport = getattr(client, "transport", None)
                    if transport and transport.venue in saved:
                        transport.restore(saved[transport.venue])
            except (OSError, ValueError, TypeError):
                raise GridError("Cannot read saved market cooldowns") from None
        self.reference = ReferenceCache(self.var, experiment.output / "quote-cache.json", experiment.pricing, write_export,
                                        experiment.previous_output / "quote-cache.json" if experiment.previous_output else None) if experiment.pricing.mode == "shared_indicative_v1" else None

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
                return self._next_shared(cohort) if self.reference else self._next(cohort)
        finally:
            # Persist even when Lighter fails before a frame can be published.
            # The ledger reset deliberately leaves this transport state intact.
            self.save_cooldowns()

    def _next_shared(self, cohort):
        q = self.lighter_snapshot(cohort)
        var, status = self.reference.read()
        now = time.time()
        # Quote retrieval may take time; never refresh the source timestamp.
        var, status = self.reference.status(now, cache_used=status["cache_used"], error=status["refresh_error"],
                                          error_kind=status["refresh_error_kind"])
        reason = q.get("reason", "")
        if not q["ready"] or not -2 <= now - q["ts"] <= self.experiment.settings.max_quote_age_seconds:
            var, reason = None, reason or "QQQ observation delayed; account known fills and defer new decisions"
        elif var is None:
            reason = status["refresh_error"] or "US100 缓存过期或市场状态不可用，等待有效报价"
        if q.get("close_only", False) or (var or {}).get("close_only", False):
            reason = "Venue in close-only mode; maker entries paused"
        if var:
            var = reference_price(var, self.experiment.pricing)
        market = {"lighter": q, "var": var, "reason": reason, "pricing_policy": asdict(self.experiment.pricing),
                  "quote_cache": status,
                  "allow_entries": bool(var and q["ready"] and not q["gap"] and not q.get("close_only", False) and not var["close_only"])}
        frame = cohort.prepare_frame(now, market)
        if var:
            for name, plan in frame.plans.items():
                change = abs(dec(plan["target"]) - dec(cohort.stores[name].account()["us100"]["qty"]))
                if change and dec(var["min_qty"]) <= change <= dec(var["max_qty"]):
                    key = format(change.normalize(), "f")
                    frame.quotes[key] = {**var, "qty": key, "source_qty": var["qty"],
                                         "pricing_mode": self.experiment.pricing.mode, "cache_used": status["cache_used"]}
        frame.validate(self.experiment)
        return frame

    def _next(self, cohort):
        q = self.lighter_snapshot(cohort)
        reason, var, var_state, closes_at = q.get("reason", ""), None, "unknown", None
        try:
            var = self.var.market()
            closes_at = var.get("closes_at")
            var_state = "closed" if not var.get("market_open", True) else "close_only" if var.get("close_only", False) else "open"
            if not var.get("market_open", True):
                reason, var = "US100 market closed; maker entries paused", None
        except MarketClosed as error:
            reason, var_state = str(error), "closed"
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
        market = {"lighter": q, "var": var, "var_market_state": var_state, "var_market_closes_at": closes_at,
                  "allow_entries": bool(var and q["ready"] and not q["gap"] and not q.get("close_only", False) and not var.get("close_only", False)), "reason": reason}
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
            except SessionUnavailable as error:
                market.update(var=None, reason=str(error), allow_entries=False)
                break
            except MarketClosed as error:
                market.update(var=None, var_market_state="closed", reason=str(error), allow_entries=False)
                break
            except GridError as error:
                market.update(reason=str(error), allow_entries=False)
                self._quote_cursor = (names.index(name) + 1) % len(names)
                break
            self._quote_cursor = (names.index(name) + 1) % len(names)
        # Network latency is observable. Rebuild targets at the final observation time;
        # use only quotes actually fetched for the resulting exact signed change.
        now = time.time()
        observer = getattr(self.var, "market_observation", None)
        if observer:
            observed = observer(now)
            market.update(var_market_state=observed["market_state"], var_market_closes_at=observed.get("market_closes_at"))
        if var and var.get("closes_at") is not None and now >= var["closes_at"]:
            market["var_market_state"] = "closed"
        if market["var_market_state"] == "closed":
            market.update(var=None, allow_entries=False, reason="US100 market closed; both legs paused")
        if now - q["ts"] > settings.max_quote_age_seconds or (var and (now - var["ts"] > settings.max_quote_age_seconds or abs(q["ts"] - var["ts"]) > settings.max_pair_skew_seconds)):
            market.update(var=None, allow_entries=False, reason="US100 quote expired while gathering hedge prices")
        frame = cohort.prepare_frame(now, market)
        if market["var"]:
            needed = {format(abs(dec(plan["target"]) - dec(cohort.stores[name].account()["us100"]["qty"])).normalize(), "f")
                      for name, plan in frame.plans.items()}
            for key, quote in collected.items():
                if key in needed and key != "0" and quote and -2 <= now - quote["ts"] <= settings.max_quote_age_seconds and abs(q["ts"] - quote["ts"]) <= settings.max_pair_skew_seconds:
                    frame.quotes[key] = quote
        frame.validate(self.experiment)
        return frame

    def lighter_snapshot(self, cohort):
        try:
            return self.lighter.snapshot()
        except GridError as error:
            # A failed book read must not prevent a known closure cancelling orders.
            # The last journal supplies valuation only, with its original source time.
            row = cohort.db.execute("SELECT payload FROM frames ORDER BY ts DESC LIMIT 1").fetchone()
            if row is None:
                raise
            q = QQQFrame.decode(row[0]).market["lighter"]
            q = {**q, "ready": False, "gap": True, "trades": [], "reason": str(error)}
            observer = getattr(self.lighter, "market_observation", None)
            if observer:
                state = observer(time.time())
                q["metadata_ts"] = state["market_source_ts"]
                if state["market_state"] != "unknown":
                    q.update(market_open=state["market_state"] != "closed", close_only=state["market_state"] == "close_only")
            return q


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


def read_qqq_dashboard(experiment, window, *, include_history=True, include_details=True, history_deadline=None, through_ts=None):
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
        raw = db.execute("SELECT payload FROM summaries" + (" WHERE ts<=?" if through_ts is not None else "")
                         + " ORDER BY ts DESC LIMIT 1", (through_ts,) if through_ts is not None else ()).fetchone()
        if not raw:
            return result
        summary = result["summary"] = decode_summary(raw[0])
        names = [r["name"] for r in summary["scenarios"]]
        # Ledgers retain only the last two account snapshots. Capture this
        # published point before a long history scan lets the writer prune it.
        details = include_details
        for name, config in experiment.scenarios.items() if include_details else ():
            with closing(read_db(config.state_file)) as ledger:
                ledger.execute("BEGIN")
                raw = ledger.execute("SELECT account FROM ticks WHERE ts=?", (summary["ts"],)).fetchone()
                if not raw:
                    # A concurrent writer can prune this checkpoint. Never mix
                    # newer account state with the already published summary.
                    details = False
                    break
                account = json.loads(raw[0])
                result["positions"].extend({"scenario": name, **slot} for slot in account["slots"] if dec(slot["qty"]) > 0)
                for raw, in ledger.execute("SELECT payload FROM fills WHERE frame_ts<=? ORDER BY id DESC LIMIT 100", (summary["ts"],)):
                    result["trades"].append({"scenario": name, **json.loads(raw)})
        if include_history:
            from .qqq_history import read_history
            result["history"] = {"range": window, **read_history(
                db, summary["ts"] - WINDOWS[window], summary["ts"], names,
                experiment.settings.poll_seconds, summary["scenarios"][0].get("hedge_threshold_usdc") is not None,
                deadline=history_deadline)}
    if not details:
        result["positions"], result["trades"] = [], []
    result["trades"].sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
    result["details_available"] = details
    return result


def read_qqq_snapshot(experiment):
    """Fast published state, independent of the exclusive history/reset lock.

    Each database uses a read transaction; reset-state readback prevents a
    response from combining data across archive/clear/generation transitions.
    All early returns and read failures pass through the same epoch check.
    """
    from .reset import read_state
    for _ in range(2):
        before = read_state(experiment)
        try:
            result = read_qqq_dashboard(experiment, "24h", include_history=False)
        except (OSError, sqlite3.Error, GridError, ValueError, KeyError, TypeError):
            if before != read_state(experiment):
                continue
            raise
        after = read_state(experiment)
        if before == result["reset"] == after:
            return result
    raise GridError("QQQ snapshot changed during reset; retry")


def read_qqq_history(experiment, window, through_ts=None):
    from .reset import control_lock
    # Retain reset exclusion for the long read; current snapshots do not wait
    # for this lock. Bound abandoned requests rather than scanning indefinitely.
    with control_lock(experiment):
        result = read_qqq_dashboard(experiment, window, include_details=False,
                                    history_deadline=time.monotonic() + 25, through_ts=through_ts)
    return {"reset": result["reset"], "summary_ts": (result["summary"] or {}).get("ts"),
            "history": result["history"]}


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
                        "size_step": ".000001", "min_qty": ".000004", "max_qty": "10000", "market_open": True,
                        "symbol": "US100S", "instrument_type": "swap", "multiplier": "1", "quantity_unit": "index_unit",
                        "close_only": False, "closes_at": ts + 3600, "metadata_ts": ts, "received_ts": ts, "source_ts": ts}
            source = reference_price(quote(".01"), experiment.pricing)
            market = {"lighter": q, "var": source, "allow_entries": True, "reason": "Synthetic demonstration; not a historical backtest",
                      "pricing_policy": asdict(experiment.pricing), "quote_cache": {**asdict(experiment.pricing),
                      "source_ts": ts, "source_qty": ".01", "available": True, "cache_used": False, "age_seconds": 0, "refresh_error": ""}}
            frame = cohort.prepare_frame(ts, market, data_kind="synthetic")
            for name, plan in frame.plans.items():
                amount = abs(dec(plan["target"]) - dec(cohort.stores[name].account()["us100"]["qty"]))
                if amount >= D(".000004"):
                    frame.quotes[format(amount.normalize(), "f")] = {**source, "qty": str(amount), "source_qty": ".01", "pricing_mode": experiment.pricing.mode, "cache_used": False}
            cohort.ingest(frame)
        emit({"demo": "synthetic_not_backtest", "experiments": str(path), "accounts": len(experiment.scenarios),
              "scenarios": [{k: r[k] for k in ("name", "total_pnl_usdc", "turnover_usdc")} for r in cohort.latest()["scenarios"]]})
