"""Upgrade the original three absolute-step experiments without rewriting any ledger."""
import json
import os
from pathlib import Path
import tempfile

from .comparison import Experiment
from .models import GridError, dec

LEGACY = {"step-0.15": ("0.15", "0.5"), "step-0.20": ("0.20", "1"), "step-0.25": ("0.25", "2")}


def upgrade_experiment(path):
    path = Path(path).resolve()
    original = path.read_bytes()
    data = json.loads(original.decode("utf-8-sig"))
    rows = data.get("scenarios", [])
    if len(rows) != 3 or {r.get("name") for r in rows} != set(LEGACY):
        return None
    for row in rows:
        overrides = row.get("overrides", {})
        if overrides.get("grid_step_percent") is not None or dec(overrides.get("grid_step_usdc_per_barrel", "0")) != dec(LEGACY[row["name"]][0]):
            return None
    previous = Experiment.load(path)
    # Preserve all sizing/cost settings; only the requested spacing and cohort change.
    for row in rows:
        percent = LEGACY[row["name"]][1]
        row["name"] = f"step-{percent}pct"
        row["overrides"].pop("grid_step_usdc_per_barrel")
        row["overrides"]["grid_step_percent"] = percent
    old_output = Path(data["output_dir"])
    new_name = "comparison-pct-05-1-2" if old_output.name == "comparison-015-020-025" else old_output.name + "-pct-05-1-2"
    data["output_dir"] = str(old_output.with_name(new_name))
    backup = path.with_name(path.stem + ".absolute-015-020-025" + path.suffix)
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
