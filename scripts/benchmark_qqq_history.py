"""Compare synthetic dashboard history reads; never open application data.

Run from the repository root: python scripts/benchmark_qqq_history.py
Timings and tracemalloc peaks use separate passes; no performance assertions.
"""
import argparse
import base64
from contextlib import closing
import gc
import json
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from variational_grid.qqq_history import read_history


def original(db, end, names):
    """The previous reader's full-window construction and sampling."""
    points, segment, previous = [], 0, None
    for ts, raw in db.execute("SELECT ts,payload FROM summaries WHERE ts>=? AND ts<=? ORDER BY ts", (0, end)):
        data = json.loads(raw)
        history = data["history"] if data.get("qqq_compact") == 1 else {
            "gap": data["market"].get("gap", False), "pnl": [r["total_pnl_usdc"] for r in data["scenarios"]],
            "exposure": [r["signed_exposure_percent"] for r in data["scenarios"]],
            "net_exposure": [r["net_exposure_usdc"] for r in data["scenarios"]]}
        if previous is not None and (ts - previous > 15 or history["gap"]):
            segment += 1
        points.append({"ts": ts, "segment": segment, "pnl": [float(v) for v in history["pnl"]],
                       "exposure": [float(v) for v in history["exposure"]],
                       "net_exposure": [float(v) for v in history["net_exposure"]]})
        previous = ts
    count = len(points)
    if count > 900:
        selected = {0, count - 1}
        buckets = max(1, 898 // (4 * len(names) + 2))
        for bucket in range(buckets):
            lo, hi = bucket * count // buckets, (bucket + 1) * count // buckets
            selected.update((lo, hi - 1))
            for key in ("pnl", "net_exposure"):
                for column in range(len(names)):
                    selected.add(min(range(lo, hi), key=lambda i: points[i][key][column]))
                    selected.add(max(range(lo, hi), key=lambda i: points[i][key][column]))
        points = [points[i] for i in sorted(selected)]
    return {"names": names, "source_count": count, "points": points}


def generate(rows, width, state_bytes, legacy_every, old_compact):
    state = "".join(random.Random(0).choices("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", k=state_bytes))
    for index in range(rows):
        history = {"gap": index % 7919 == 0,
                   "pnl": [str((index * (j + 3)) % 137 - 68) for j in range(width)],
                   "exposure": [str((index * (j + 5)) % 101 - 50) for j in range(width)],
                   "net_exposure": [str((index * (j + 7)) % 419 - 209) for j in range(width)]}
        record = {"market": {"gap": history["gap"]}, "account_state": state, "scenarios": [
            {"name": str(j), "total_pnl_usdc": history["pnl"][j],
             "signed_exposure_percent": history["exposure"][j], "net_exposure_usdc": history["net_exposure"][j]}
            for j in range(width)]}
        if not legacy_every or index % legacy_every:
            packed = base64.b64encode(zlib.compress(json.dumps(record, separators=(",", ":")).encode(), 3)).decode()
            record = {"qqq_compact": 1, "history": history if old_compact else {**history, "names": [str(j) for j in range(width)]},
                      "state": packed}
        yield index * 2, json.dumps(record, separators=(",", ":"))


def measured(db, operation, repeat):
    timings = []
    for _ in range(repeat):
        gc.collect()
        db.execute("BEGIN")
        try:
            started = time.perf_counter()
            result = operation()
            timings.append(time.perf_counter() - started)
        finally:
            db.rollback()
    gc.collect()
    tracemalloc.start()
    db.execute("BEGIN")
    try:
        operation()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        db.rollback()
    return result, {"seconds_median": statistics.median(timings), "seconds_samples": timings,
                    "python_peak_bytes": peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=50000)
    parser.add_argument("--accounts", type=int, default=3)
    parser.add_argument("--state-bytes", type=int, default=4096)
    parser.add_argument("--legacy-every", type=int, default=10)
    parser.add_argument("--old-compact", action="store_true", help="Benchmark unlabeled historical compact records requiring state decompression")
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()
    if args.rows < 1 or not 1 <= args.accounts <= 20 or args.state_bytes < 0 or args.legacy_every < 0 or args.repeat < 1:
        parser.error("Require positive rows/repeat, 1..20 accounts and nonnegative state bytes/legacy interval")
    names = [str(j) for j in range(args.accounts)]
    with tempfile.TemporaryDirectory(prefix="qqq-history-benchmark-") as directory:
        path = Path(directory) / "synthetic.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE summaries(ts INTEGER PRIMARY KEY,payload TEXT)")
            with db:
                db.executemany("INSERT INTO summaries VALUES (?,?)", generate(args.rows, args.accounts, args.state_bytes, args.legacy_every, args.old_compact))
            old, old_stats = measured(db, lambda: original(db, args.rows * 2, names), args.repeat)
            new, new_stats = measured(db, lambda: read_history(db, 0, args.rows * 2, names, 2, True), args.repeat)
            print(json.dumps({"python": platform.python_version(), "platform": platform.system(),
                              "sqlite": sqlite3.sqlite_version, "fixture": vars(args), "database_bytes": path.stat().st_size,
                              "original": old_stats, "streaming": new_stats, "identical_output": new == old,
                              "selected_points": len(new["points"]), "source_count": new["source_count"],
                              "note": "Synthetic temporary database; peak measures Python allocations, not SQLite native caches."}, indent=2))


if __name__ == "__main__":
    main()
