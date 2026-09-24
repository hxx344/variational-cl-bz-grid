"""Exercise the actual offline deployment preflight with native Python."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class DeployCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="grid-preflight-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "release"
        self.root.mkdir()
        project = Path(__file__).resolve().parents[1]
        shutil.copytree(project / "variational_grid", self.root / "variational_grid", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("deploy_check.py", "pyproject.toml", "config.example.json", "experiments.example.json",
                     "inventory.example.json", "qqq-hedge.example.json"):
            shutil.copyfile(project / name, self.root / name)

    def check(self, expected=0):
        result = subprocess.run([sys.executable, "-B", str(self.root / "deploy_check.py")],
                                cwd=self.temp.name, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_preflight_is_offline_and_leaves_user_files_and_release_untouched(self):
        with (self.root / "variational_grid/__init__.py").open("a") as source:
            source.write("\nimport socket\n"
                         "class OfflineSocket(socket.socket):\n"
                         "    def connect(self, *args): raise AssertionError('Network forbidden in preflight')\n"
                         "    def connect_ex(self, *args): raise AssertionError('Network forbidden in preflight')\n"
                         "socket.socket = OfflineSocket\n")
        # Deliberately invalid user files must never be loaded by release checks.
        (self.root / "config.local.json").write_bytes(b"user configuration sentinel")
        (self.root / "session.json").write_bytes(b"user session sentinel")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_release.py").write_text("raise AssertionError('Do not run full tests')\n")
        original = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.check()
        self.assertIn("Quick release preflight passed", result.stdout)
        self.assertEqual({p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, original)
        self.assertFalse((self.root / "data").exists())

    def test_invalid_source_is_rejected_before_imports(self):
        with (self.root / "variational_grid/engine.py").open("a") as source:
            source.write("\ndef broken(:\n")
        self.assertIn("SyntaxError", self.check(expected=1).stderr)

    def test_missing_runtime_import_is_rejected(self):
        with (self.root / "variational_grid/engine.py").open("a") as source:
            source.write("\nimport missing_grid_deployment_dependency\n")
        self.assertIn("ModuleNotFoundError", self.check(expected=1).stderr)

    def test_semantically_invalid_example_is_rejected(self):
        path = self.root / "qqq-hedge.example.json"
        path.write_text(path.read_text().replace('"3000"', '"-1"'))
        self.assertIn("Invalid QQQ hedge experiment", self.check(expected=1).stderr)

    def test_missing_dashboard_asset_is_rejected(self):
        (self.root / "variational_grid/web/qqq.js").unlink()
        self.assertIn("FileNotFoundError", self.check(expected=1).stderr)


if __name__ == "__main__":
    unittest.main()
