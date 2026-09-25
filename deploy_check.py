"""Fast offline release preflight. Full regression tests belong in CI."""
from contextlib import closing
import importlib
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import tomllib


def main():
    started = time.monotonic()
    sys.dont_write_bytecode = True
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11+ required")
    root = Path(__file__).resolve().parent
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if project["project"]["name"] != "variational-grid":
        raise RuntimeError("Unexpected release package")

    sources = sorted((root / "variational_grid").rglob("*.py"))
    for path in sources:
        compile(path.read_bytes(), str(path), "exec")
    for path in sources:
        if path.name == "__main__.py":
            continue  # Compiled above; importing it would start the CLI.
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        importlib.import_module(".".join(parts))

    from variational_grid.cli import configuration
    from variational_grid.comparison import Experiment

    # Load examples with the real parsers, without touching user config or ledgers.
    with tempfile.TemporaryDirectory(prefix="grid-preflight-") as directory:
        examples = Path(directory)
        for name in ("config.example.json", "experiments.example.json", "inventory.example.json", "qqq-hedge.example.json"):
            shutil.copyfile(root / name, examples / name)
        shutil.copyfile(examples / "config.example.json", examples / "config.local.json")
        configuration(examples / "config.example.json")
        for name in ("experiments.example.json", "inventory.example.json", "qqq-hedge.example.json"):
            Experiment.load(examples / name)

    for name in ("index.html", "app.js", "model.js", "styles.css", "inventory.html", "inventory.js", "inventory.css",
                 "qqq.html", "qqq.js", "qqq.css", "var-session.js", "hub.js"):
        if not (root / "variational_grid/web" / name).read_text(encoding="utf-8").strip():
            raise RuntimeError(f"Empty dashboard asset: {name}")
    with closing(sqlite3.connect(":memory:")) as db:
        if db.execute("SELECT json_extract(?, '$.ready')", ('{"ready":1}',)).fetchone() != (1,):
            raise RuntimeError("SQLite JSON support required")
    print(f"Quick release preflight passed in {time.monotonic() - started:.2f}s (imports, examples, assets, SQLite). Full tests run in CI.")


if __name__ == "__main__":
    main()
