"""Upgrade known comparison defaults to +/-30% without rewriting any ledger."""
import json
import os
from pathlib import Path
import tempfile

from .comparison import Experiment
from .models import GridError, dec

LEGACY = {"step-0.15": ("0.15", "0.5"), "step-0.20": ("0.20", "1"), "step-0.25": ("0.25", "2")}
PERCENT = {"step-0.5pct": "0.5", "step-1pct": "1", "step-2pct": "2"}


def upgrade_experiment(path):
    path = Path(path).resolve()
    original = path.read_bytes()
    data = json.loads(original.decode("utf-8-sig"))
    rows = data.get("scenarios", [])
    names = {r.get("name") for r in rows}
    old_output = Path(data["output_dir"])
    # This version already applied; preserve subsequent user edits on repeated installs.
    if old_output.name.endswith(("-range30", "-range30-center3d")):
        return None
    absolute = names == set(LEGACY)
    if len(rows) != 3 or not (absolute or names == set(PERCENT)):
        return None
    for row in rows:
        overrides = row.get("overrides", {})
        if absolute:
            matches = overrides.get("grid_step_percent") is None and dec(overrides.get("grid_step_usdc_per_barrel", "0")) == dec(LEGACY[row["name"]][0])
        else:
            value = overrides.get("grid_step_percent")
            matches = value is not None and dec(value) == dec(PERCENT[row["name"]])
        if not matches:
            return None
    previous = Experiment.load(path)
    if not absolute and all(dec(c.grid_step_percent) * c.max_levels == 30 for c in previous.scenarios.values()):
        return None
    # Keep quantity, capital and costs; change the requested grid geometry only.
    for row in rows:
        percent = LEGACY[row["name"]][1] if absolute else PERCENT[row["name"]]
        row["name"] = f"step-{percent}pct"
        row["overrides"].pop("grid_step_usdc_per_barrel", None)
        row["overrides"]["grid_step_percent"] = percent
        row["overrides"]["max_levels"] = int(dec("30") / dec(percent))
    new_name = "comparison-pct-05-1-2-range30" if old_output.name in ("comparison-015-020-025", "comparison-pct-05-1-2") else old_output.name + ("-pct-05-1-2-range30" if absolute else "-range30")
    data["output_dir"] = str(old_output.with_name(new_name))
    backup = path.with_name(path.stem + (".absolute-015-020-025" if absolute else ".before-range30") + path.suffix)
    if backup.exists() and backup.read_bytes() != original:
        raise GridError("Legacy experiment backup already exists with different settings; refusing to overwrite it")
    handle, temporary = tempfile.mkstemp(prefix=".experiment-upgrade-", suffix=".json", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        proposed = Experiment.load(temporary)
        if proposed.output == previous.output:
            raise GridError("Percentage experiments must use a separate output directory")
        # A migration must start a new comparison, never inherit unknown existing data.
        if proposed.output.exists() and any(proposed.output.iterdir()):
            raise GridError("New percentage experiment directory is not empty; existing data left untouched")
        if not backup.exists():
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            backup.chmod(path.stat().st_mode & 0o777)
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
        return backup
    finally:
        temporary.unlink(missing_ok=True)


def upgrade_center(path, *, comparison=True):
    """Version old center settings into a separate run, leaving all old files intact."""
    from .cli import configuration
    path = Path(path).resolve()
    original = path.read_bytes()
    data = json.loads(original.decode("utf-8-sig"))
    current = Experiment.load(path) if comparison else configuration(path)
    legacy_saved_center = False
    if comparison:
        manifest = current.output / "experiment.json"
        if manifest.exists():
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            # A --single upgrade may already have changed the shared base config.
            # The existing comparison's durable identity is the authority for its window.
            legacy_saved_center = any(c.get("center_hours", 168) != 72 for c in saved["scenarios"].values())
    if current.center_hours == 72 and not legacy_saved_center:
        return None
    data["center_hours"] = 72
    key = "output_dir" if comparison else "state_file"
    old = Path(data[key])
    new = old.with_name(old.name + "-center3d") if comparison else old.with_name(old.stem + "-center3d" + old.suffix)
    target = (path.parent / new).resolve()
    if target.exists():
        raise GridError("Three-day target already exists; old data and configuration preserved")
    data[key] = str(new)
    backup = path.with_name(path.stem + ".before-center3d" + path.suffix)
    if backup.exists() and backup.read_bytes() != original:
        raise GridError("Three-day backup already contains different settings")
    handle, temporary = tempfile.mkstemp(prefix=".center-upgrade-", suffix=".json", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        (Experiment.load if comparison else configuration)(temporary)
        if not backup.exists():
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            backup.chmod(path.stat().st_mode & 0o777)
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
        return backup
    finally:
        temporary.unlink(missing_ok=True)
