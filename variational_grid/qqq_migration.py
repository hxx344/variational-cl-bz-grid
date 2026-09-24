"""Move recognized nine-account defaults into a separate three-account run."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

from .models import GridError, dec
from .qqq_comparison import QQQExperiment
from .qqq_hedge import QQQSettings


VERSION = "-usd3000-hs0015-v1"


def upgrade_qqq_defaults(path):
    path = Path(path).resolve()
    original = path.read_bytes()
    data = json.loads(original.decode("utf-8-sig"))
    expected = {f"grid-{step}-hedge-{band}": (step, band) for step in ("0.05", "0.1", "0.2") for band in ("0", "2", "5")}
    rows = data.get("scenarios", [])
    if len(rows) != 9 or {r.get("name") for r in rows} != set(expected):
        return None
    previous = QQQExperiment.load(path)
    if (asdict(previous.settings) != asdict(QQQSettings()) or Path(data["output_dir"]).name.endswith(VERSION)
            or previous.pricing.mode != "shared_indicative_v1" or previous.pricing.half_spread_percent is not None):
        return None  # Preserve explicitly customized economics and later edits.
    for row in rows:
        step, band = expected[row["name"]]
        if row.get("hedge_tolerance_percent") is None or dec(row["grid_step_percent"]) != dec(step) or dec(row["hedge_tolerance_percent"]) != dec(band):
            return None
    data["strategy"]["var_slippage_bps"] = "0"
    data["scenarios"] = [{"name": f"grid-{step}-hedge-3000usd", "grid_step_percent": step, "hedge_threshold_usdc": "3000"}
                         for step in ("0.05", "0.1", "0.2")]
    pricing = data.setdefault("pricing", {})
    pricing.update(mode="shared_indicative_v1", half_spread_percent="0.0015")
    pricing.setdefault("refresh_after_seconds", 3)
    pricing.setdefault("max_age_seconds", 60)
    old_output = Path(data["output_dir"])
    data["previous_output_dir"] = str(previous.output)
    data["output_dir"] = str(old_output.with_name(old_output.name + VERSION))
    backup = path.with_name(path.stem + ".before-usd3000-hs0015-v1" + path.suffix)
    if backup.exists() and backup.read_bytes() != original:
        raise GridError("QQQ migration backup differs; existing configuration preserved")
    handle, temporary = tempfile.mkstemp(prefix=".qqq-upgrade-", suffix=".json", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        proposed = QQQExperiment.load(temporary)
        if proposed.output == previous.output or proposed.output.exists() and any(proposed.output.iterdir()):
            raise GridError("New QQQ experiment directory is not empty; existing data preserved")
        if not backup.exists():
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            backup.chmod(path.stat().st_mode & 0o777)
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return backup
