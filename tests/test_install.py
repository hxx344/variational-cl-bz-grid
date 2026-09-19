"""Run the real installer against a temporary Linux filesystem and local Git source.

Only package installation, account/service management and the remote session check
are stubbed. Git, archive extraction, config validation and release switching run.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "linux", "Installer integration requires Linux")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="grid-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Path(__file__).resolve().parents[1]
        self.source = self.root / "repository"
        self.source.mkdir()
        shutil.copytree(self.project / "variational_grid", self.source / "variational_grid", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("config.example.json", "experiments.example.json"):
            shutil.copy(self.project / name, self.source / name)
        (self.source / "tests").mkdir()
        (self.source / "tests/test_release.py").write_text(
            "import unittest\nclass ReleaseTest(unittest.TestCase):\n    def test_release(self): self.assertTrue(True)\n")
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Installer fixture")
        self.git("config", "user.email", "installer@example.invalid")
        self.revision = self.commit()
        self.app = self.root / "opt/variational-grid"
        self.conf = self.root / "etc/variational-grid"
        self.state = self.root / "var/lib/variational-grid"
        units = self.root / "etc/systemd/system"
        units.mkdir(parents=True)
        self.unit = units / "variational-grid.service"
        installer = (self.project / "install.sh").read_text()
        for original, replacement in (
            ("/opt/variational-grid", self.app),
            ("/etc/variational-grid", self.conf),
            ("/var/lib/variational-grid", self.state),
            ("/etc/systemd/system", units),
            ("https://github.com/hxx344/variational-cl-bz-grid.git", self.source),
        ):
            installer = installer.replace(original, str(replacement))
        # CI need not be root. All absolute install targets above are in this temp dir.
        installer = installer.replace("if [[ ${EUID} -ne 0 ]]; then", "if false; then")
        installer = installer.replace("</dev/tty", "</dev/null")  # Interactive import is stubbed below.
        self.script = self.root / "install.sh"
        self.script.write_text(installer)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        shim = f'''#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["GRID_INSTALL_TEST_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "install":
    filtered = []
    i = 0
    while i < len(args):
        if args[i] in ("-o", "-g"):
            i += 2
        else:
            filtered.append(args[i]); i += 1
    os.execv("/usr/bin/install", ["install", *filtered])
if name == "runuser":
    if args[3:7] != ["python3", "-m", "variational_grid", "check-session"]:
        if args[3:7] == ["python3", "-m", "variational_grid", "init-session"] and os.environ.get("GRID_INSTALL_TEST_MISSING_SESSION"):
            print("Fixture: hidden session import completed")
        else:
            raise SystemExit("Unexpected credential operation in installer test")
    elif os.environ.get("GRID_INSTALL_TEST_MISSING_SESSION"):
        # Execute the actual missing-file error path, which cannot contact the API.
        raise SystemExit(subprocess.call(args[3:]))
'''
        for name in ("apt-get", "id", "useradd", "install", "runuser", "systemctl"):
            path = self.bin / name
            path.write_text(shim)
            path.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "GRID_INSTALL_TEST_LOG": str(self.log), "PYTHONDONTWRITEBYTECODE": "1"}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args], stderr=subprocess.STDOUT, text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-m", "Test release")
        return self.git("rev-parse", "HEAD")

    def install(self, *args, expected=0):
        result = subprocess.run(["bash", str(self.script), *args], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_fresh_install_and_upgrade_preserve_configuration_data_and_mode(self):
        self.install()
        self.assertEqual((self.conf / "mode").read_text().strip(), "compare")
        self.assertIn("compare --experiments", self.unit.read_text())
        self.assertEqual((self.app / "current").resolve().name, self.revision)
        experiments = json.loads((self.conf / "experiments.json").read_text())
        self.assertEqual([s["overrides"]["grid_step_usdc_per_barrel"] for s in experiments["scenarios"]], ["0.15", "0.20", "0.25"])
        config = json.loads((self.conf / "config.json").read_text())
        config["paper_balance_usdc"] = "1500"
        (self.conf / "config.json").write_text(json.dumps(config))
        experiments["scenarios"][0]["overrides"]["max_levels"] = 6
        (self.conf / "experiments.json").write_text(json.dumps(experiments))
        preserved = {self.conf / "config.json": (self.conf / "config.json").read_bytes(),
                     self.conf / "experiments.json": (self.conf / "experiments.json").read_bytes(),
                     self.state / "ledger-sentinel": b"original paper data", self.state / "session.json": b"fixture-only"}
        for path, content in preserved.items():
            path.write_bytes(content)
        (self.state / "session.json").chmod(0o600)
        (self.source / "release-marker").write_text("upgrade")
        updated = self.commit()
        self.install()
        self.assertEqual((self.app / "current").resolve().name, updated)
        self.assertTrue((self.app / "releases" / self.revision).is_dir())
        self.install()  # Repeat the identical release as well.
        for path, content in preserved.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual((self.state / "session.json").stat().st_mode & 0o777, 0o600)
        self.install("--single")
        self.install()
        self.assertEqual((self.conf / "mode").read_text().strip(), "run")
        self.assertIn(" run --config ", self.unit.read_text())
        self.install("--compare")
        self.assertIn(" compare --experiments ", self.unit.read_text())
        for path, content in preserved.items():
            self.assertEqual(path.read_bytes(), content)

    def test_failed_release_does_not_switch_or_restart_existing_service(self):
        self.install()
        original_unit = self.unit.read_bytes()
        (self.source / "tests/test_release.py").write_text(
            "import unittest\nclass ReleaseTest(unittest.TestCase):\n    def test_release(self): self.fail('injected failure')\n")
        failed_revision = self.commit()
        self.log.write_text("")
        self.install(expected=1)
        self.assertEqual((self.app / "current").resolve().name, self.revision)
        self.assertEqual(self.unit.read_bytes(), original_unit)
        self.assertFalse((self.app / "releases" / failed_revision).exists())
        self.assertFalse(list((self.app / "releases").glob(".staging.*")))
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertFalse(any(call[0] == "systemctl" for call in calls))

    def test_help_and_bad_arguments_do_not_change_the_system(self):
        help_result = self.install("--help")
        self.assertIn("0.15 / 0.20 / 0.25", help_result.stdout)
        self.install("--unknown", expected=1)
        self.install("--compare", "--single", expected=1)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.app.exists())

    def test_missing_session_prompts_cleanly_then_completes_installation(self):
        self.env["GRID_INSTALL_TEST_MISSING_SESSION"] = "1"
        result = self.install()
        self.assertIn("Cannot read session", result.stdout)
        self.assertIn("public candles and statistics do not require one", result.stdout)
        self.assertIn("Fixture: hidden session import completed", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertNotIn("sys.excepthook", result.stdout + result.stderr)
        self.assertIn("compare --experiments", self.unit.read_text())


if __name__ == "__main__":
    unittest.main()
