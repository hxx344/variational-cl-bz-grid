"""Loopback paper monitor with queued resets; never reads a login token or calls the venue."""
from contextlib import closing
import base64
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from .comparison import Experiment, Frame, quantity_key
from .engine import Engine
from .models import GridError, dec
from .store import fill_totals
from .reset import control_lock, read_state, request_reset

WINDOWS = {"1h": 3600, "24h": 86400, "7d": 604800}
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/model.js": ("model.js", "text/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8")}


def read_db(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)


def reduce_points(points, limit=900):
    """Bound SVG work while retaining each bucket's endpoints and every series' extrema."""
    if len(points) <= limit:
        return points
    dimensions = len(points[0]["pnl"]) + 2
    buckets = max(1, (limit - 2) // (2 * dimensions + 2))
    selected = {0, len(points) - 1}
    for bucket in range(buckets):
        start, end = bucket * len(points) // buckets, (bucket + 1) * len(points) // buckets
        selected.update((start, end - 1))
        for dim in range(dimensions):
            def value(i):
                p = points[i]
                return [p["spread"], p["center"], *p["pnl"]][dim]
            valid = [i for i in range(start, end) if value(i) is not None]
            if valid:
                selected.add(min(valid, key=value))
                selected.add(max(valid, key=value))
    return [points[i] for i in sorted(selected)]


def read_history(db, summary, window):
    names = [r["name"] for r in summary["scenarios"]]
    columns = ["ts", "COALESCE(json_extract(payload,'$.center'),json_extract(payload,'$.center_7d'))", "json_extract(payload,'$.scenarios[0].spread_bz_minus_cl')"]
    for i in range(len(names)):
        columns += [f"json_extract(payload,'$.scenarios[{i}].name')", f"json_extract(payload,'$.scenarios[{i}].total_pnl_usdc')"]
    rows = db.execute(f"SELECT {','.join(columns)} FROM summaries WHERE ts>=? AND ts<=? ORDER BY ts",
                      (summary["ts"] - WINDOWS[window], summary["ts"]))
    points, previous, segment = [], None, 0
    gap = max(60, summary["poll_seconds"] * 3)
    for row in rows:
        if previous is not None and row[0] - previous > gap:
            segment += 1
        values = dict(zip(row[3::2], row[4::2]))
        points.append({"ts": row[0], "center": float(row[1]), "spread": float(row[2]), "segment": segment,
                       "pnl": [float(values[n]) if values.get(n) is not None else None for n in names]})
        previous = row[0]
    return {"range": window, "names": names, "source_count": len(points), "points": reduce_points(points)}


def read_dashboard(experiment, window="24h"):
    with control_lock(experiment):
        if window not in WINDOWS:
            raise GridError("Unknown history window")
        if getattr(experiment, "kind", None) == "inventory":
            from .inventory_comparison import read_inventory_dashboard
            return read_inventory_dashboard(experiment, window)
        return _read_dashboard(experiment, window)


def _read_dashboard(experiment, window):
    if window not in WINDOWS:
        raise GridError("Unknown history window")
    result = {"version": 1, "server_ts": time.time(), "runtime": {"status": "starting"}, "summary": None,
              "center_window_hours": experiment.center_hours, "reset": read_state(experiment),
              "positions": [], "trades": [], "details_available": False,
              "trade_limit_per_scenario": 100, "history": {"range": window, "names": [], "source_count": 0, "points": []}}
    if result["reset"] and result["reset"]["status"] in {"archiving", "clearing"}:
        result["runtime"] = {"status": "resetting"}
        return result
    database = experiment.output / "comparison.sqlite3"
    if not database.exists():
        return result
    with closing(read_db(database)) as db:
        db.execute("BEGIN")
        runtime = db.execute("SELECT payload FROM runtime WHERE id=1").fetchone()
        if runtime:
            # No filesystem paths, process ids, credential objects or raw exceptions in this API.
            raw = json.loads(runtime[0])
            result["runtime"] = {k: raw.get(k) for k in ("status", "reason", "updated_utc")}
        latest = db.execute("SELECT payload FROM summaries ORDER BY ts DESC LIMIT 1").fetchone()
        if latest is None:
            return result
        summary = result["summary"] = json.loads(latest[0])
        summary.setdefault("center", summary.get("center_7d"))
        summary.setdefault("center_window_hours", 168)
        result["history"] = read_history(db, summary, window)
        raw_frame = db.execute("SELECT payload FROM frames WHERE ts=?", (summary["ts"],)).fetchone()
        frame = Frame.decode(raw_frame[0]) if raw_frame else None
    if frame is None:
        return result
    result["details_available"] = True
    for row in summary["scenarios"]:
        name = row["name"]
        config = experiment.scenarios.get(name)
        if config is None:
            raise GridError("Dashboard configuration differs from published experiment")
        row.update(paper_leverage=config.paper_leverage, max_margin_fraction=config.max_margin_fraction,
                   max_holding_hours=config.max_holding_hours, grid_step_percent=config.grid_step_percent,
                   grid_step=str(config.grid_step(frame.center)))
        row.update(config.grid_geometry(frame.center))
        row.update(Engine(config, None).position_limits(row["equity_usdc"], row["margin_usdc"]))
        # Value with the economics recorded in the ledger, never silently with edited config.
        with closing(read_db(config.state_file)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            identity = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
            if identity is None or json.loads(identity[0]) != json.loads(config.strategy_identity()):
                raise GridError("Dashboard settings differ from the saved experiment")
            last_tick = db.execute("SELECT value FROM meta WHERE key='last_tick'").fetchone()
            if last_tick is None or float(last_tick[0]) < frame.ts:
                raise GridError("Scenario ledger is older than the published comparison")
            if "volume_barrels" not in row:
                # Read-only compatibility: a ledger may be ahead of the last shared frame.
                row.update(fill_totals(db, through=frame.ts))
            positions = db.execute("SELECT * FROM lots WHERE opened<=? AND (closed IS NULL OR closed>?) ORDER BY level,id",
                                   (frame.ts, frame.ts)).fetchall()
            valuation = Engine(config, None)
            for record in positions:
                lot = dict(record)
                pnl = valuation.exit_value(lot, *frame.quotes[quantity_key(config.quantity_barrels)])[0] - dec(lot["entry_fee"])
                result["positions"].append({"scenario": name, **{k: lot[k] for k in ("id", "direction", "level", "qty", "entry_center", "entry_cl", "entry_bz", "entry_fee", "opened")},
                                            "unrealized_pnl_usdc": str(pnl), "valued_at": frame.ts,
                                            "target_pnl_usdc": str(dec(lot["qty"]) * config.grid_step(lot["entry_center"]))})
            trades = db.execute("SELECT * FROM lots WHERE closed IS NOT NULL AND closed<=? ORDER BY closed DESC,id DESC LIMIT 100", (frame.ts,)).fetchall()
            for record in trades:
                result["trades"].append({"scenario": name, **dict(record)})
    result["trades"].sort(key=lambda r: (r["closed"], r["id"]), reverse=True)
    return result


def make_server(experiment, port=9876):
    assets = Path(__file__).with_name("web")
    routes = dict(ASSETS)
    if getattr(experiment, "kind", None) == "inventory":
        routes["/"] = ("inventory.html", "text/html; charset=utf-8")
        routes["/inventory.js"] = ("inventory.js", "text/javascript; charset=utf-8")
        routes["/inventory.css"] = ("inventory.css", "text/css; charset=utf-8")
    routes["/index.html"] = routes["/"]
    reset_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "GridMonitor"

        def log_message(self, *_):
            pass

        def reply(self, status, content, content_type="text/plain; charset=utf-8", head=False, report=False):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            hashes = {tag: "" for tag in ("script", "style")}
            if report:
                # The existing, locally generated report is self-contained.
                for tag in hashes:
                    hashes[tag] = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(block).digest()).decode() + "'"
                                           for block in re.findall(rb"<" + tag.encode() + rb">(.*?)</" + tag.encode() + rb">", content, re.S))
            self.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'self' {hashes['script']}; style-src 'self' {hashes['style']}; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            if not head:
                self.wfile.write(content)

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_GET(self, head=False):
            try:
                host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            except ValueError:
                host = None
            if host not in {"127.0.0.1", "localhost"}:
                return self.reply(403, b"Use a localhost SSH tunnel", head=head)
            parsed = urlsplit(self.path)
            try:
                if parsed.path in routes:
                    filename, mime = routes[parsed.path]
                    return self.reply(200, (assets / filename).read_bytes(), mime, head)
                if parsed.path == "/api/dashboard":
                    query = parse_qs(parsed.query)
                    window = query.get("range", ["24h"])[0]
                    if window not in WINDOWS or set(query) - {"range"}:
                        return self.reply(400, b"Invalid history window", head=head)
                    data = read_dashboard(experiment, window)
                    data["reset_token"] = reset_token
                    payload = json.dumps(data, ensure_ascii=False).encode()
                    return self.reply(200, payload, "application/json; charset=utf-8", head)
                if parsed.path == "/report":
                    # HTML parsers normalize newlines before checking inline CSP hashes.
                    page = (experiment.output / "public/index.html").read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    return self.reply(200, page, "text/html; charset=utf-8", head, report=True)
                return self.reply(404, b"Not found", head=head)
            except (OSError, sqlite3.Error, GridError, ValueError, KeyError, TypeError):
                return self.reply(503, b'{"error":"Dashboard data temporarily unavailable"}', "application/json", head)

        def do_POST(self):
            if self.path != "/api/reset":
                return self.reply(405, b"Unsupported operation")
            host = self.headers.get("Host", "")
            try:
                parsed_host = urlsplit("http://" + host)
                valid_host = parsed_host.hostname in {"localhost", "127.0.0.1"} and not parsed_host.username and not parsed_host.path
            except ValueError:
                valid_host = False
            origin = self.headers.get("Origin")
            if (not valid_host or origin is not None and origin != "http://" + host
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or not secrets.compare_digest(self.headers.get("X-Reset-Token", ""), reset_token)):
                return self.reply(403, b"Same-origin reset request required")
            if self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding"):
                return self.reply(400, b"JSON request required")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 512:
                    raise ValueError()
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {"generation"}:
                    raise ValueError()
                state = request_reset(experiment, body["generation"])
                self.reply(202, json.dumps({"reset": state}).encode(), "application/json")
            except GridError:
                self.reply(409, b'{"error":"Simulation changed or is busy; refresh and retry"}', "application/json")
            except (ValueError, TypeError):
                self.reply(400, b"Invalid reset request")
            except (OSError, sqlite3.Error):
                self.reply(503, b'{"error":"Reset request could not be saved"}', "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve_dashboard(args):
    from .cli import emit
    if not 0 <= args.port <= 65535:
        raise GridError("Dashboard port must be between 0 and 65535")
    experiment = Experiment.load(args.experiments)
    with make_server(experiment, args.port) as server:
        emit({"event": "dashboard", "url": f"http://127.0.0.1:{server.server_port}", "paper_reset_enabled": True})
        server.serve_forever()
