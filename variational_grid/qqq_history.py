"""Bounded dashboard history reads inside the caller's SQLite snapshot."""
import base64
import json
import sqlite3
import time
import zlib

from .models import GridError


class HistoryTimeout(GridError):
    pass


def history_rows(db, start, end):
    """Keep the compact record's compressed account state out of Python."""
    suffix = "FROM summaries WHERE ts>=? AND ts<=? ORDER BY ts"
    try:
        rows = db.execute("SELECT ts, CASE WHEN json_extract(payload,'$.qqq_compact')=1 "
                          "AND json_type(payload,'$.history.names')='array' "
                          "THEN json_extract(payload,'$.history') ELSE payload END " + suffix,
                          (start, end))
    except sqlite3.OperationalError as error:
        if "no such function: json_extract" not in str(error):
            raise
        # Some system SQLite builds omit JSON functions. Still stream one record.
        rows = db.execute("SELECT ts,payload " + suffix, (start, end))
    for ts, raw in rows:
        data = json.loads(raw)
        if data.get("qqq_compact") == 1:
            history = data["history"]
            if "names" not in history:
                # Historical compact arrays have no account labels. The matching
                # state is the only reliable source; configuration order can change.
                try:
                    state = json.loads(zlib.decompress(base64.b64decode(data["state"])))
                    history["names"] = [row["name"] for row in state["scenarios"]]
                except (ValueError, TypeError, KeyError, zlib.error):
                    raise ValueError("Invalid unlabeled QQQ compact history state") from None
            data = history
        elif "scenarios" in data:
            data = {"names": [row["name"] for row in data["scenarios"]],
                    "gap": data["market"].get("gap", False),
                    "pnl": [row["total_pnl_usdc"] for row in data["scenarios"]],
                    "exposure": [row["signed_exposure_percent"] for row in data["scenarios"]],
                    "net_exposure": [row.get("net_exposure_usdc") for row in data["scenarios"]]}
        yield ts, data


def read_history(db, start, end, names, poll_seconds, dollar_hedge, limit=900, *, deadline=None):
    """Preserve bucket endpoints/extrema without retaining the full window.

    COUNT and iteration must share the read transaction already used for the
    published summary. Equal extrema retain their first index, as before.
    """
    if not db.in_transaction:
        raise ValueError("History requires the dashboard read transaction")
    count = db.execute("SELECT COUNT(*) FROM summaries WHERE ts>=? AND ts<=?", (start, end)).fetchone()[0]
    keys = ("pnl", "net_exposure" if dollar_hedge else "exposure")
    width = len(names)
    buckets = max(1, (limit - 2) // (4 * width + 2)) if count > limit else 0
    selected, segment, previous = [], 0, None
    last_missing = (-1,) * (len(keys) * width)
    gap_seconds, missing_values = max(15, poll_seconds * 3), [None] * width
    bucket, candidates = 0, {}
    extrema = [[[None, None, None, None] for _ in names] for _ in keys]
    lo, hi = 0, count // buckets if buckets else count
    for index, (ts, history) in enumerate(history_rows(db, start, end)):
        if deadline is not None and index % 64 == 0 and time.monotonic() >= deadline:
            raise HistoryTimeout("QQQ history read exceeded its time budget")
        if previous is not None and (ts - previous > gap_seconds or history["gap"]):
            segment += 1
        point = {"ts": ts, "segment": segment}
        record_names = history["names"]
        if record_names == names:
            order = None
        else:
            if len(record_names) != width or set(record_names) != set(names):
                raise ValueError("History account names differ from the published summary")
            order = [record_names.index(name) for name in names]
        for key in ("pnl", "exposure", "net_exposure"):
            values = history.get(key, missing_values)
            if len(values) != width:
                raise ValueError("History series does not match its account names")
            if order is not None:
                values = [values[column] for column in order]
            point[key] = [float(value) if value is not None else None
                          for value in values]
        previous = ts
        if not buckets:
            selected.append(point)
            continue
        missing = None in point[keys[0]] or None in point[keys[1]]
        if missing:
            latest = list(last_missing)
            for series, key in enumerate(keys):
                for column, value in enumerate(point[key]):
                    if value is None:
                        latest[series * width + column] = index
            last_missing = tuple(latest)
        item = (index, point, last_missing)
        if index == lo or index == hi - 1:
            candidates[index] = item
        for key, series in zip(keys, extrema):
            for value, bounds in zip(point[key], series):
                if value is None:
                    continue
                if bounds[0] is None or value < bounds[0]:
                    bounds[0], bounds[1] = value, item
                if bounds[2] is None or value > bounds[2]:
                    bounds[2], bounds[3] = value, item
        if index == hi - 1:
            for series in extrema:
                for bounds in series:
                    for extreme in (bounds[1], bounds[3]):
                        if extreme is not None:
                            candidates[extreme[0]] = extreme
            selected.extend(candidates[i] for i in sorted(candidates))
            candidates = {}
            extrema = [[[None, None, None, None] for _ in names] for _ in keys]
            bucket += 1
            lo, hi = hi, (bucket + 1) * count // buckets
    if buckets:
        points, extra_segments, prior = [], 0, None
        for item in selected:
            index, point, holes = item
            original_segment = point["segment"]
            if prior is not None and original_segment == prior[1] and any(
                    holes[series * width + column] > prior[0]
                    and value is not None and prior[2][key][column] is not None
                    for series, key in enumerate(keys) for column, value in enumerate(point[key])):
                # If a dropped legacy/missing sample broke a series, never draw
                # a line across it. A shared segment conservatively splits all
                # series; selected numeric values and extrema remain unchanged.
                extra_segments += 1
            point["segment"] += extra_segments
            points.append(point)
            prior = index, original_segment, point
        selected = points
    return {"names": names, "source_count": count, "points": selected}
