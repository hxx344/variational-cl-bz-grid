"""Move recognized ladder/v1 defaults into a separate distance-free scalper run."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile

from .models import GridError, dec
from .qqq_comparison import QQQExperiment
from .qqq_hedge import QQQSettings
from .qqq_scalper import CURRENT_MODEL, ScalperSettings


VERSION = "-scalper-v2"


def upgrade_qqq_defaults(path):
    path = Path(path).resolve()
    original = path.read_bytes()
    data = json.loads(original.decode("utf-8-sig"))
    expected = {f"grid-{step}-hedge-{band}": (step, band) for step in ("0.05", "0.1", "0.2") for band in ("0", "2", "5")}
    rows = data.get("scenarios", [])
    three = {f"grid-{step}-hedge-3000usd": (step, "3000") for step in ("0.05", "0.1", "0.2")}
    names = {r.get("name") for r in rows}
    nine = len(rows) == 9 and names == set(expected)
    if not (nine or len(rows) == 3 and names == set(three)):
        return None
    previous = QQQExperiment.load(path)
    if previous.scalper is not None and (nine or previous.scalper != ScalperSettings()):
        return None  # Latest model and customized scalper timing are preserved.
    defaults = QQQSettings() if nine else replace(QQQSettings(), var_slippage_bps="0")
    if (asdict(previous.settings) != asdict(defaults) or Path(data["output_dir"]).name.endswith(VERSION)
            or previous.pricing.mode != "shared_indicative_v1"
            or previous.pricing.half_spread_percent != (None if nine else "0.0015")):
        return None  # Preserve explicitly customized economics and later edits.
    expected = expected if nine else three
    band_key = "hedge_tolerance_percent" if nine else "hedge_threshold_usdc"
    for row in rows:
        step, band = expected[row["name"]]
        if row.get(band_key) is None or dec(row["grid_step_percent"]) != dec(step) or dec(row[band_key]) != dec(band):
            return None
        if previous.scalper is not None and dec(row.get("take_profit_percent", step)) != dec(step):
            return None
    data["strategy"]["var_slippage_bps"] = "0"
    data["scalper"] = asdict(ScalperSettings(model=CURRENT_MODEL))
    data["scenarios"] = [{"name": f"grid-{step}-hedge-3000usd", "grid_step_percent": step,
                          "take_profit_percent": step, "hedge_threshold_usdc": "3000"}
                         for step in ("0.05", "0.1", "0.2")]
    pricing = data.setdefault("pricing", {})
    pricing.update(mode="shared_indicative_v1", half_spread_percent="0.0015")
    pricing.setdefault("refresh_after_seconds", 3)
    pricing.setdefault("max_age_seconds", 60)
    old_output = Path(data["output_dir"])
    source = previous.output
    if previous.previous_output and not any((source / name).exists() for name in ("quote-cache.json", "market-cooldowns.json")):
        source = previous.previous_output
    data["previous_output_dir"] = str(source)
    data["output_dir"] = str(old_output.with_name(old_output.name + VERSION))
    backup = path.with_name(path.stem + ".before" + VERSION + path.suffix)
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
