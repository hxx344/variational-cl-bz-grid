"""Durable paper reset requests; only the existing simulation writer clears ledgers."""
from contextlib import closing
import json
import os
import sqlite3
import time
import uuid

from .models import GridError, utc
from .store import ProcessLock

BUSY = {"pending", "archiving", "clearing"}


def sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def control_lock(experiment):
    return ProcessLock(str(experiment.output) + ".control")


def read_state(experiment):
    path = experiment.output / "reset-state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_state(experiment, state):
    path = experiment.output / "reset-state.json"
    temporary = path.with_suffix(".new")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def initialize(experiment):
    with control_lock(experiment):
        if read_state(experiment) is None:
            save_state(experiment, {"generation": uuid.uuid4().hex, "status": "idle"})


def request_reset(experiment, generation):
    with control_lock(experiment):
        state = read_state(experiment)
        if state is None or not isinstance(generation, str) or generation != state["generation"]:
            raise GridError("Simulation changed; refresh before requesting a reset")
        if state["status"] in BUSY:
            return state  # Coalesce double clicks and requests from multiple tabs.
        state = {"generation": generation, "status": "pending", "request_id": uuid.uuid4().hex,
                 "requested_utc": utc(time.time())}
        save_state(experiment, state)
        return state


def backup_database(source, destination):
    temporary = destination.with_suffix(".new")
    with closing(sqlite3.connect(temporary)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise GridError("Paper archive validation failed")
    os.replace(temporary, destination)
    sync_directory(destination.parent)


def process_reset(cohort):
    """Called at a cycle boundary (and before recovery on restart). Never calls the venue."""
    experiment = cohort.experiment
    # A long read-only history request must not pause the sampling writer when
    # there is no reset to do. Requests arriving after this atomic-file read are
    # handled at the next cycle boundary; busy state is rechecked under the lock.
    state = read_state(experiment)
    if state is None or state["status"] not in BUSY:
        return False
    with control_lock(experiment):
        state = read_state(experiment)
        if state is None or state["status"] not in BUSY:
            return False
        request_id = state["request_id"]
        if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            raise GridError("Invalid paper reset request")
        archive = experiment.output / "archives" / request_id
        if not archive.resolve().is_relative_to(experiment.output.resolve()) or archive.is_symlink():
            raise GridError("Invalid paper archive location")
        if state["status"] != "clearing":
            # Finish a journaled shared frame before taking a consistent multi-ledger archive.
            cohort.recover()
            state["status"] = "archiving"
            save_state(experiment, state)
            try:
                archive.mkdir(parents=True, exist_ok=True)
                backup_database(cohort.db, archive / "comparison.sqlite3")
                ledgers = archive / "ledgers"
                ledgers.mkdir(exist_ok=True)
                for name, store in cohort.stores.items():
                    backup_database(store.db, ledgers / (name + ".sqlite3"))
                with (archive / "experiment.json").open("w", encoding="utf-8") as stream:
                    json.dump(experiment.identity(), stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Persist completion before touching any live table.
                with (archive / "complete.json").open("w", encoding="utf-8") as stream:
                    json.dump({"request_id": request_id, "archived_utc": utc(time.time())}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                for directory in (ledgers, archive, archive.parent, experiment.output):
                    sync_directory(directory)
            except (OSError, sqlite3.Error, GridError):
                state.update(status="failed", message="Archive failed; existing simulation data preserved")
                save_state(experiment, state)
                return False
            state["status"] = "clearing"
            save_state(experiment, state)
        if not (archive / "complete.json").is_file():
            raise GridError("Completed paper archive is required before reset")
        # Each clear is atomic and idempotent. A restart finishes the whole cohort before
        # it can publish or ingest another frame; dashboard reads share this control lock.
        for name, store in cohort.stores.items():
            store.reset(experiment.scenarios[name])
        with cohort.db:
            for table in ("frames", "summaries", "runtime"):
                cohort.db.execute("DELETE FROM " + table)
        cohort.set_runtime("starting", "Paper simulation reset; waiting for a new shared quote")
        state.update(status="complete", generation=request_id, completed_utc=utc(time.time()), archive_id=request_id)
        save_state(experiment, state)
        return True


def reset_comparison(args):
    from .cli import emit
    from .comparison import Cohort, Experiment
    if not args.confirm:
        raise GridError("Use --confirm to archive and reset every paper scenario")
    experiment = Experiment.load(args.experiments)
    if getattr(experiment, "kind", None) == "qqq_hedge":
        from .qqq_comparison import QQQCohort
        Cohort = QQQCohort
    if getattr(experiment, "kind", None) == "inventory":
        from .inventory_comparison import InventoryCohort
        Cohort = InventoryCohort
    state = read_state(experiment)
    if state is None:
        raise GridError("No initialized simulation to reset")
    request_reset(experiment, state["generation"])
    try:
        # Offline reset obtains the same writer lock. A running writer consumes the request.
        with Cohort(experiment):
            pass
    except GridError as error:
        if str(error) != "Another process already owns this ledger":
            raise
    emit({"reset": read_state(experiment)})
