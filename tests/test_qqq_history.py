"""History equivalence, bounded output and snapshot-consistent streaming."""
from contextlib import closing
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from variational_grid.dashboard import make_server, read_dashboard
from variational_grid.models import GridError
from variational_grid.qqq_comparison import read_qqq_dashboard, summary_record
from variational_grid.qqq_history import read_history


def sample(index, width=3, *, gap=False, missing=False):
    return {"gap": gap,
            "pnl": [str(((index * (j + 3)) % 137) - 68) for j in range(width)],
            "exposure": [str(((index * (j + 5)) % 101) - 50) for j in range(width)],
            **({} if missing else {"net_exposure": [str(((index * (j + 7)) % 419) - 209) for j in range(width)]})}


def payload(history, *, compact=True, state="unused", old_compact=False, names=None):
    names = names or [str(j) for j in range(len(history["pnl"]))]
    summary = {"market": {"gap": history["gap"]}, "padding": state, "scenarios": [
        {"name": names[j], "total_pnl_usdc": pnl, "signed_exposure_percent": history["exposure"][j],
         **({"net_exposure_usdc": history["net_exposure"][j]} if "net_exposure" in history else {})}
        for j, pnl in enumerate(history["pnl"])]}
    if compact:
        return json.dumps({"qqq_compact": 1, "history": history if old_compact else {**history, "names": names},
                           "state": base64.b64encode(zlib.compress(json.dumps(summary).encode())).decode()})
    return json.dumps(summary)


def original_points(records, width, dollar=True, poll=2):
    """Frozen pre-change algorithm, for histories whose fields all exist."""
    points, segment, previous = [], 0, None
    for ts, history in records:
        if previous is not None and (ts - previous > max(15, poll * 3) or history["gap"]):
            segment += 1
        points.append({"ts": ts, "segment": segment,
                       **{key: list(map(float, history[key])) for key in ("pnl", "exposure", "net_exposure")}})
        previous = ts
    count = len(points)
    if count > 900:
        selected = {0, count - 1}
        buckets = max(1, 898 // (4 * width + 2))
        for bucket in range(buckets):
            lo, hi = bucket * count // buckets, (bucket + 1) * count // buckets
            selected.update((lo, hi - 1))
            for key in ("pnl", "net_exposure" if dollar else "exposure"):
                for column in range(width):
                    selected.add(min(range(lo, hi), key=lambda i: points[i][key][column]))
                    selected.add(max(range(lo, hi), key=lambda i: points[i][key][column]))
        points = [points[i] for i in sorted(selected)]
    return points


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE summaries(ts REAL PRIMARY KEY,payload TEXT)")

    def save(self, records, mixed=False):
        with self.db:
            self.db.executemany("INSERT INTO summaries VALUES (?,?)",
                                ((ts, payload(history, compact=not mixed or i % 2 == 0))
                                 for i, (ts, history) in enumerate(records)))

    def read(self, start=0, end=1e9, width=3, dollar=True, db=None):
        db = db or self.db
        db.execute("BEGIN")
        try:
            return read_history(db, start, end, [str(i) for i in range(width)], 2, dollar)
        finally:
            db.rollback()

    def test_empty_and_inclusive_window_with_exactly_900_samples(self):
        self.assertEqual(self.read()["points"], [])
        records = [(i, sample(i)) for i in range(902)]
        self.save(records)
        result = self.read(1, 900)
        self.assertEqual(result["source_count"], 900)
        self.assertEqual(result["points"], original_points(records[1:901], 3))

    def test_streamed_buckets_equal_original_for_mixed_formats_and_both_units(self):
        for width in (1, 3, 9, 20):
            records = [(i * 2 + (90 if i > 1700 else 0), sample(i, width, gap=i % 541 == 0))
                       for i in range(5001)]
            with self.db:
                self.db.execute("DELETE FROM summaries")
            self.save(records, mixed=True)
            for dollar in (True, False):
                with self.subTest(width=width, dollar=dollar):
                    result = self.read(width=width, dollar=dollar)
                    self.assertEqual(result["points"], original_points(records, width, dollar))
                    self.assertEqual(result["source_count"], len(records))
                    self.assertLessEqual(len(result["points"]), 900)

    def test_every_accounts_global_extrema_and_both_endpoints_survive(self):
        records = [(i, sample(i)) for i in range(10001)]
        for column in range(3):
            for key in ("pnl", "net_exposure"):
                records[103 + column * 100][1][key][column] = "-1000000"
                records[7903 + column * 100][1][key][column] = "1000000"
        self.save(records)
        points = self.read()["points"]
        self.assertEqual((points[0]["ts"], points[-1]["ts"]), (0, 10000))
        for key in ("pnl", "net_exposure"):
            for column in range(3):
                self.assertEqual(min(p[key][column] for p in points), -1000000)
                self.assertEqual(max(p[key][column] for p in points), 1000000)

    def test_discarded_gap_samples_still_split_selected_points(self):
        history = {"gap": False, "pnl": ["0"], "exposure": ["0"], "net_exposure": ["0"]}
        records = [(i, {**history, "gap": i == 1501}) for i in range(10000)]
        self.save(records)
        points = self.read(width=1)["points"]
        before = max((p for p in points if p["ts"] < 1501), key=lambda p: p["ts"])
        after = min((p for p in points if p["ts"] > 1501), key=lambda p: p["ts"])
        self.assertNotEqual(before["segment"], after["segment"])
        self.assertEqual(points, original_points(records, 1))

    def test_old_compact_and_full_summaries_keep_missing_net_exposure_as_none(self):
        records = [(i, sample(i, missing=i % 2 == 0)) for i in range(40)]
        self.save(records, mixed=True)
        points = self.read()["points"]
        self.assertEqual(points[0]["net_exposure"], [None] * 3)
        self.assertEqual(points[2]["net_exposure"], [None] * 3)
        # Include a full legacy summary without the formerly mandatory field.
        with self.db:
            self.db.execute("UPDATE summaries SET payload=? WHERE ts=3", (payload(sample(3, missing=True), compact=False),))
        self.assertEqual(self.read()["points"][3]["net_exposure"], [None] * 3)

    def test_account_order_is_aligned_across_legacy_old_compact_and_new_compact(self):
        values = {"gap": False, "pnl": ["10", "20"], "exposure": ["1", "2"], "net_exposure": ["100", "200"]}
        reversed_values = {key: list(reversed(value)) if isinstance(value, list) else value for key, value in values.items()}
        with self.db:
            for ts, compact, old in ((1, False, False), (2, True, True), (3, True, False)):
                self.db.execute("INSERT INTO summaries VALUES (?,?)",
                                (ts, payload(values, compact=compact, old_compact=old, names=["1", "0"])))
                self.db.execute("INSERT INTO summaries VALUES (?,?)",
                                (ts + 3, payload(reversed_values, compact=compact, old_compact=old, names=["0", "1"])))
        result = self.read(width=2)
        for point in result["points"]:
            self.assertEqual(point["pnl"], [20, 10])
            self.assertEqual(point["exposure"], [2, 1])
            self.assertEqual(point["net_exposure"], [200, 100])

    def test_unlabeled_compact_without_valid_state_is_rejected_instead_of_guessing(self):
        with self.db:
            self.db.execute("INSERT INTO summaries VALUES (1,?)", (json.dumps({"qqq_compact": 1,
                            "history": sample(1), "state": "invalid-state"}),))
        with self.assertRaises((ValueError, zlib.error)):
            self.read()

    def test_labeled_history_with_mismatched_accounts_or_lengths_is_rejected(self):
        for history in ({**sample(1), "names": ["0", "0", "2"]},
                        {**sample(1), "names": ["0", "1", "2"], "pnl": ["1"]}):
            with self.db:
                self.db.execute("DELETE FROM summaries")
                self.db.execute("INSERT INTO summaries VALUES (1,?)", (json.dumps({"qqq_compact": 1,
                                "history": history, "state": "not-needed-for-labeled-history"}),))
            with self.assertRaises(ValueError):
                self.read()

    def test_dropped_missing_interval_never_connects_valid_extrema(self):
        history = {"gap": False, "pnl": ["0"], "exposure": ["0"], "net_exposure": ["0"]}
        records = [(i, {k: v for k, v in history.items() if k != "net_exposure" or i != 1501}) for i in range(10000)]
        self.save(records, mixed=True)
        points = self.read(width=1)["points"]
        self.assertNotIn(1501, [p["ts"] for p in points])
        before = max((p for p in points if p["ts"] < 1501), key=lambda p: p["ts"])
        after = min((p for p in points if p["ts"] > 1501), key=lambda p: p["ts"])
        self.assertNotEqual(before["segment"], after["segment"])
        self.assertEqual(before["net_exposure"], [0.0])
        self.assertEqual(after["net_exposure"], [0.0])
        self.assertLessEqual(len(points), 900)

    def test_entire_legacy_window_missing_net_exposure_remains_bounded(self):
        self.save(((i, sample(i, missing=True)) for i in range(10000)), mixed=True)
        points = self.read()["points"]
        self.assertLessEqual(len(points), 900)
        self.assertTrue(all(p["net_exposure"] == [None] * 3 for p in points))
        self.assertTrue(all(p["segment"] == 0 for p in points))  # Available PnL remains continuous.

    def test_compact_state_is_not_materialized_by_python_json_parser(self):
        with self.db:
            self.db.execute("INSERT INTO summaries VALUES (?,?)", (1, payload(sample(1), state="X" * 200000)))
        parse = json.loads
        observed = []
        def capture(raw):
            observed.append(len(raw))
            self.assertNotIn('"state"', raw)
            return parse(raw)
        with patch("variational_grid.qqq_history.json.loads", side_effect=capture):
            self.assertEqual(self.read()["source_count"], 1)
        self.assertLess(max(observed), 1000)

    def test_missing_sqlite_json_extension_streams_legacy_and_compact_fallback(self):
        records = [(i, sample(i)) for i in range(1000)]
        self.save(records, mixed=True)
        inner = self.db
        class WithoutJSON:
            in_transaction = True
            def execute(self, sql, args=()):
                if "json_extract" in sql:
                    raise sqlite3.OperationalError("no such function: json_extract")
                return inner.execute(sql, args)
            def rollback(self):
                inner.rollback()
        self.assertEqual(self.read(db=WithoutJSON())["points"], original_points(records, 3))

    def test_read_without_transaction_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "read transaction"):
            read_history(self.db, 0, 1, ["a"], 2, True)

    def test_count_and_rows_share_snapshot_during_concurrent_wal_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with closing(sqlite3.connect(path)) as writer, closing(sqlite3.connect(path)) as reader:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE summaries(ts REAL PRIMARY KEY,payload TEXT)")
                with writer:
                    writer.execute("INSERT INTO summaries VALUES (1,?)", (payload(sample(1)),))
                class ConcurrentCommit:
                    in_transaction = True
                    def execute(self, sql, args=()):
                        cursor = reader.execute(sql, args)
                        if "COUNT(*)" in sql:
                            with writer:
                                writer.execute("INSERT INTO summaries VALUES (2,?)", (payload(sample(2)),))
                        return cursor
                    def rollback(self):
                        reader.rollback()
                result = self.read(db=ConcurrentCommit())
                self.assertEqual(result["source_count"], 1)
                self.assertEqual([p["ts"] for p in result["points"]], [1])
                self.assertEqual(self.read(db=reader)["source_count"], 2)

    def test_dashboard_integrates_history_with_latest_summary_and_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "experiment.json").write_text("{}")
            experiment = SimpleNamespace(output=root, settings=SimpleNamespace(poll_seconds=2),
                                         scenarios={}, identity=lambda: {})
            with closing(sqlite3.connect(root / "comparison.sqlite3")) as db, db:
                db.executescript("CREATE TABLE runtime(id,payload); CREATE TABLE summaries(ts PRIMARY KEY,payload);")
                for ts in (399, 400, 401, 4000):
                    row = {"ts": ts, "market": {"gap": False}, "scenarios": [{"name": "a", "total_pnl_usdc": "1",
                           "signed_exposure_percent": "2", "net_exposure_usdc": "3", "hedge_threshold_usdc": "3000"}]}
                    db.execute("INSERT INTO summaries VALUES (?,?)", (ts, summary_record(row)))
            result = read_qqq_dashboard(experiment, "1h")
            self.assertEqual(result["history"]["source_count"], 3)
            self.assertEqual(result["history"]["range"], "1h")
            self.assertEqual([p["ts"] for p in result["history"]["points"]], [400, 401, 4000])
            self.assertEqual(result["summary"]["ts"], 4000)

    def test_dashboard_aligns_swapped_accounts_using_each_records_own_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "experiment.json").write_text("{}")
            experiment = SimpleNamespace(output=root, settings=SimpleNamespace(poll_seconds=2),
                                         scenarios={}, identity=lambda: {})
            with closing(sqlite3.connect(root / "comparison.sqlite3")) as db, db:
                db.executescript("CREATE TABLE runtime(id,payload); CREATE TABLE summaries(ts PRIMARY KEY,payload);")
                for ts, names in ((1, ["a", "b"]), (2, ["b", "a"]), (3, ["a", "b"]), (4, ["b", "a"])):
                    row = {"ts": ts, "market": {"gap": False}, "scenarios": [{"name": name,
                           "total_pnl_usdc": "10" if name == "a" else "20", "signed_exposure_percent": "0",
                           "net_exposure_usdc": "100" if name == "a" else "200", "hedge_threshold_usdc": "3000"}
                           for name in names]}
                    record = json.loads(summary_record(row))
                    self.assertEqual(record["history"]["names"], names)
                    if ts == 1:
                        del record["history"]["names"]
                    elif ts == 2:
                        record = row
                    db.execute("INSERT INTO summaries VALUES (?,?)", (ts, json.dumps(record)))
            history = read_qqq_dashboard(experiment, "1h")["history"]
            self.assertEqual(history["names"], ["b", "a"])
            self.assertTrue(all(point["pnl"] == [20, 10] for point in history["points"]))
            self.assertTrue(all(point["net_exposure"] == [200, 100] for point in history["points"]))

    def detail_fixture(self, root):
        output = root / "paper"
        output.mkdir()
        (output / "experiment.json").write_text("{}")
        configs = {name: SimpleNamespace(state_file=output / (name + ".sqlite3")) for name in ("a", "b")}
        experiment = SimpleNamespace(kind="qqq_hedge", output=output, settings=SimpleNamespace(poll_seconds=2),
                                     scenarios=configs, identity=lambda: {})
        row = {"ts": 100, "market": {"gap": False}, "scenarios": [{"name": name, "total_pnl_usdc": "1",
               "signed_exposure_percent": "2", "net_exposure_usdc": "3", "hedge_threshold_usdc": "3000"}
               for name in configs]}
        with closing(sqlite3.connect(output / "comparison.sqlite3")) as db, db:
            db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE runtime(id,payload); CREATE TABLE summaries(ts PRIMARY KEY,payload);")
            db.execute("INSERT INTO summaries VALUES (100,?)", (summary_record(row),))
        for config in configs.values():
            with closing(sqlite3.connect(config.state_file)) as db, db:
                db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE ticks(ts PRIMARY KEY,account); CREATE TABLE fills(id PRIMARY KEY,frame_ts,payload);")
                for identity, ts, qty in ((1, 100, "1"), (2, 102, "99")):
                    db.execute("INSERT INTO ticks VALUES (?,?)", (ts, json.dumps({"slots": [{"slot": 1, "qty": qty}]})))
                    db.execute("INSERT INTO fills VALUES (?,?,?)", (identity, ts, json.dumps({"id": identity, "ts": ts, "qty": qty})))
        return experiment

    def test_long_history_read_keeps_published_details_after_writer_prunes_old_ticks(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = self.detail_fixture(Path(directory))
            original = read_history
            def advance_while_reading(db, *args, **kwargs):
                self.assertTrue(db.in_transaction)
                for config in experiment.scenarios.values():
                    with closing(sqlite3.connect(config.state_file)) as writer, writer:
                        for ts in (104, 106):
                            writer.execute("INSERT INTO ticks VALUES (?,?)", (ts, json.dumps({"slots": [{"slot": 1, "qty": "200"}]})))
                        writer.execute("DELETE FROM ticks WHERE ts<104")
                        writer.execute("INSERT INTO fills VALUES (3,106,?)", (json.dumps({"id": 3, "ts": 106, "qty": "200"}),))
                        self.assertIsNone(writer.execute("SELECT account FROM ticks WHERE ts=100").fetchone())
                return original(db, *args, **kwargs)
            with patch("variational_grid.qqq_history.read_history", side_effect=advance_while_reading) as history:
                result = read_dashboard(experiment, "1h")
            history.assert_called_once()
            self.assertEqual(result["summary"]["ts"], 100)
            self.assertTrue(result["details_available"])
            self.assertEqual({p["scenario"] for p in result["positions"]}, {"a", "b"})
            self.assertTrue(all(p["qty"] == "1" for p in result["positions"]))
            self.assertEqual(len(result["trades"]), 2)
            self.assertTrue(all(t["id"] == 1 and t["ts"] == 100 for t in result["trades"]))
            self.assertEqual(result["history"]["points"][-1]["ts"], 100)

    def test_missing_published_snapshot_keeps_summary_without_future_or_partial_details(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = self.detail_fixture(Path(directory))
            with closing(sqlite3.connect(experiment.scenarios["b"].state_file)) as db, db:
                db.execute("DELETE FROM ticks WHERE ts=100")
            result = read_dashboard(experiment, "1h")
            self.assertEqual(result["summary"]["ts"], 100)
            self.assertFalse(result["details_available"])
            self.assertEqual(result["positions"], [])
            self.assertEqual(result["trades"], [])
            self.assertEqual(result["history"]["source_count"], 1)

    def test_corrupt_compressed_latest_or_historical_state_returns_clean_503(self):
        bad = json.dumps({"qqq_compact": 1, "history": sample(1, 2),
                          "state": base64.b64encode(b"private-payload-not-zlib").decode()})
        for corrupt_latest in (False, True):
            with self.subTest(latest=corrupt_latest), tempfile.TemporaryDirectory() as directory:
                experiment = self.detail_fixture(Path(directory))
                with closing(sqlite3.connect(experiment.output / "comparison.sqlite3")) as db, db:
                    if corrupt_latest:
                        db.execute("UPDATE summaries SET payload=? WHERE ts=100", (bad,))
                    else:
                        db.execute("INSERT INTO summaries VALUES (99,?)", (bad,))
                # Exercise the actual HTTP handler without opening a network socket.
                with patch("variational_grid.dashboard.ThreadingHTTPServer",
                           side_effect=lambda address, handler: SimpleNamespace(Handler=handler)):
                    server = make_server(experiment)
                request = object.__new__(server.Handler)
                request.headers = {"Host": "localhost"}
                request.path = "/api/dashboard"
                request.reply = Mock()
                request.do_GET()
                request.reply.assert_called_once_with(
                    503, b'{"error":"Dashboard data temporarily unavailable"}', "application/json", False)


if __name__ == "__main__":
    unittest.main()
