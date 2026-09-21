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
        for name in ("config.example.json", "experiments.example.json", "install.sh", "pyproject.toml"):
            shutil.copy(self.project / name, self.source / name)
        (self.source / "tests").mkdir()
        (self.source / "tests/test_release.py").write_text(
            "import os, unittest\nfrom pathlib import Path\nclass ReleaseTest(unittest.TestCase):\n"
            "    def test_release(self):\n"
            "        with Path(os.environ['GRID_INSTALL_TEST_VALIDATION_LOG']).open('a') as log: log.write('validated\\n')\n")
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Installer fixture")
        self.git("config", "user.email", "installer@example.invalid")
        self.revision = self.commit()
        self.app = self.root / "opt/variational-grid"
        self.conf = self.root / "etc/variational-grid"
        self.state = self.root / "var/lib/variational-grid"
        self.proc = self.root / "proc"
        self.proc.mkdir()
        units = self.root / "etc/systemd/system"
        units.mkdir(parents=True)
        self.unit = units / "variational-grid.service"
        self.web_unit = units / "variational-grid-web.service"
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
        installer = installer.replace("Path('/proc')", f"Path({str(self.proc)!r})")
        self.script = self.root / "install.sh"
        self.script.write_text(installer)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.service_state = self.root / "services.json"
        self.validation_log = self.root / "validation.log"
        real_git = shutil.which("git")
        shim = f'''#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["GRID_INSTALL_TEST_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "git":
    os.execv({real_git!r}, ["git", *args])
if name == "df":
    kind = os.environ.get("GRID_INSTALL_TEST_LOW_STORAGE")
    if kind and args[0] in ("-Pk", "-Pi"):
        available = 0 if (kind == "bytes") == (args[0] == "-Pk") else 100000000
        print("Filesystem Blocks Used Available Use% Mounted")
        print(f"fixture 100000000 0 {{available}} 0% /")
        raise SystemExit(0)
    os.execv("/usr/bin/df", ["df", *args])
if name == "dpkg-query":
    if args[-1] in os.environ.get("GRID_INSTALL_TEST_MISSING_PACKAGES", "").split(","):
        raise SystemExit(1)
    print("installed")
if name == "systemctl":
    path = Path(os.environ["GRID_INSTALL_TEST_SERVICE_STATE"])
    state = json.loads(path.read_text()) if path.exists() else {{}}
    service = args[-1]
    row = state.setdefault(service, {{"active": False, "enabled": False}})
    if args[0] == "show":
        if os.environ.get("GRID_INSTALL_TEST_FAIL_INSPECTION"):
            raise SystemExit(1)
        print("LoadState=loaded")
        print(f"MainPID={{row.get('pid', 0) if row['active'] else 0}}")
        raise SystemExit(0)
    if args[0] == "is-active": raise SystemExit(0 if row["active"] else 3)
    if args[0] == "is-enabled": raise SystemExit(0 if row["enabled"] else 1)
    if args[0] == "daemon-reload" and os.environ.get("GRID_INSTALL_TEST_FAIL_RELOAD"):
        raise SystemExit(1)
    if args[0] == "enable": row["enabled"] = True
    if args[0] == "restart":
        row["active"] = True
        row["pid"] = 1001 if service == "variational-grid.service" else 1002
        cwd = Path({str(self.proc)!r}) / str(row["pid"]) / "cwd"
        cwd.parent.mkdir(exist_ok=True)
        cwd.unlink(missing_ok=True)
        cwd.symlink_to((Path({str(self.app)!r}) / "current").resolve(), target_is_directory=True)
    if args[0] == "disable": row.update(active=False, enabled=False)
    path.write_text(json.dumps(state))
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
        for name in ("apt-get", "df", "dpkg-query", "git", "id", "useradd", "install", "runuser", "systemctl"):
            path = self.bin / name
            path.write_text(shim)
            path.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "GRID_INSTALL_TEST_LOG": str(self.log), "PYTHONDONTWRITEBYTECODE": "1",
                    "GRID_INSTALL_TEST_SERVICE_STATE": str(self.service_state),
                    "GRID_INSTALL_TEST_VALIDATION_LOG": str(self.validation_log)}

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

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def restarts(self):
        return [call[-1] for call in self.calls() if call[:2] == ['systemctl', 'restart']]

    def test_repeat_skips_package_fetch_archive_tests_and_restarts(self):
        self.install()
        validated = self.validation_log.read_bytes()
        self.log.write_text('')
        result = self.install()
        self.assertIn('skipping full test suite', result.stdout)
        self.assertEqual(self.validation_log.read_bytes(), validated)
        self.assertFalse(any(call[0] == 'apt-get' for call in self.calls()))
        self.assertFalse(any(call[0] == 'git' and any(arg in ('fetch', 'clone', 'archive') for arg in call[1:]) for call in self.calls()))
        self.assertEqual(self.restarts(), [])
        self.assertNotIn(['systemctl', 'daemon-reload'], self.calls())
        self.assertFalse(list(self.app.glob('.deploy.*')))

    def test_only_missing_packages_are_installed(self):
        self.env['GRID_INSTALL_TEST_MISSING_PACKAGES'] = 'ca-certificates'
        self.install()
        self.assertEqual([c for c in self.calls() if c[0]=='apt-get'], [
            ['apt-get', 'update', '-qq'], ['apt-get', 'install', '-y', '-qq', 'ca-certificates']])
        self.env.pop('GRID_INSTALL_TEST_MISSING_PACKAGES')
        self.log.write_text('')
        self.install()
        self.assertFalse(any(c[0]=='apt-get' for c in self.calls()))

    def test_docs_reuse_validation_and_web_change_only_restarts_web(self):
        self.install()
        validated = self.validation_log.read_bytes()
        (self.source / 'README.md').write_text('Documentation only')
        revision = self.commit()
        self.log.write_text('')
        self.install()
        self.assertEqual((self.app / 'current').resolve().name, revision)
        self.assertEqual(self.validation_log.read_bytes(), validated)
        self.assertEqual(self.restarts(), [])
        with (self.source / 'variational_grid/web/styles.css').open('a') as file:
            file.write('\n/* New UI version */\n')
        self.commit()
        self.log.write_text('')
        self.install()
        self.assertEqual(len(self.validation_log.read_text().splitlines()), 2)
        self.assertEqual(self.restarts(), ['variational-grid-web.service'])

    def test_runtime_and_configuration_changes_restart_affected_services(self):
        self.install()
        with (self.source / 'variational_grid/engine.py').open('a') as file:
            file.write('\n# Runtime revision\n')
        self.commit()
        self.log.write_text('')
        self.install()
        self.assertEqual(set(self.restarts()), {'variational-grid.service','variational-grid-web.service'})
        validated = self.validation_log.read_bytes()
        config_path = self.conf / 'config.json'
        config = json.loads(config_path.read_text())
        config['poll_seconds'] = 20
        config_path.write_text(json.dumps(config))
        self.log.write_text('')
        self.install()
        self.assertEqual(set(self.restarts()), {'variational-grid.service','variational-grid-web.service'})
        self.assertEqual(self.validation_log.read_bytes(), validated)

    def test_inactive_service_is_repaired_without_restarting_healthy_peer(self):
        self.install()
        state = json.loads(self.service_state.read_text())
        state['variational-grid-web.service'].update(active=False, enabled=False)
        self.service_state.write_text(json.dumps(state))
        self.log.write_text('')
        self.install()
        self.assertEqual(self.restarts(), ['variational-grid-web.service'])
        self.assertIn(['systemctl', 'enable', 'variational-grid-web.service'], self.calls())

    def test_interrupted_unit_reload_is_retried(self):
        self.install()
        self.web_unit.write_text(self.web_unit.read_text() + '\n# Modified unit\n')
        self.env['GRID_INSTALL_TEST_FAIL_RELOAD'] = '1'
        self.install(expected=1)
        self.env.pop('GRID_INSTALL_TEST_FAIL_RELOAD')
        self.log.write_text('')
        self.install()
        self.assertIn(['systemctl', 'daemon-reload'], self.calls())
        self.assertEqual(self.restarts(), ['variational-grid-web.service'])

    def test_fresh_install_and_upgrade_preserve_configuration_data_and_mode(self):
        self.install()
        self.assertEqual((self.conf / "mode").read_text().strip(), "compare")
        self.assertIn("compare --experiments", self.unit.read_text())
        self.assertIn("dashboard --experiments", self.web_unit.read_text())
        self.assertIn("--port 9876", self.web_unit.read_text())
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertIn(["systemctl", "enable", "variational-grid-web.service"], calls)
        self.assertIn(["systemctl", "restart", "variational-grid-web.service"], calls)
        self.assertEqual((self.app / "current").resolve().name, self.revision)
        experiments = json.loads((self.conf / "experiments.json").read_text())
        self.assertEqual([s["overrides"]["grid_step_percent"] for s in experiments["scenarios"]], ["0.5", "1", "2"])
        self.assertEqual([s["overrides"]["max_levels"] for s in experiments["scenarios"]], [60, 30, 15])
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
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertIn(["systemctl", "disable", "--now", "variational-grid-web.service"], calls)
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
        self.assertFalse(any(call[0] == "systemctl" and call[1] != "show" for call in calls))

    def test_legacy_comparison_upgrade_archives_settings_and_keeps_old_ledgers(self):
        self.install()
        path = self.conf / 'experiments.json'
        spec = json.loads(path.read_text())
        old_output = self.state / 'comparison-015-020-025'
        old_output.mkdir()
        sentinel = old_output / 'ledger-sentinel'
        sentinel.write_bytes(b'original data')
        spec['output_dir'] = str(old_output)
        spec['scenarios'] = [{'name': f'step-{step}', 'overrides': {'grid_step_usdc_per_barrel': step, 'max_levels': 3}}
                             for step in ('0.15', '0.20', '0.25')]
        path.write_text(json.dumps(spec))
        before = path.read_bytes()
        result = self.install('--compare')
        self.assertIn('Updated grid steps to 0.5% / 1% / 2%', result.stdout)
        self.assertEqual((self.conf / 'experiments.absolute-015-020-025.json').read_bytes(), before)
        self.assertEqual(sentinel.read_bytes(), b'original data')
        updated = json.loads(path.read_text())
        self.assertEqual([s['overrides']['grid_step_percent'] for s in updated['scenarios']], ['0.5', '1', '2'])
        self.assertEqual(updated['output_dir'], str(self.state / 'comparison-pct-05-1-2-range30'))
        self.assertEqual([s['overrides']['max_levels'] for s in updated['scenarios']], [60, 30, 15])
        self.log.write_text('')
        self.install('--compare')
        self.assertEqual(self.restarts(), [])

    def test_previous_percentage_install_migrates_to_full_range(self):
        self.install()
        path = self.conf / 'experiments.json'
        spec = json.loads(path.read_text())
        spec['output_dir'] = str(self.state / 'comparison-pct-05-1-2')
        for row in spec['scenarios']:
            row['overrides'].pop('max_levels')
        path.write_text(json.dumps(spec))
        original = path.read_bytes()
        self.install('--compare')
        self.assertEqual((self.conf / 'experiments.before-range30.json').read_bytes(), original)
        updated = json.loads(path.read_text())
        self.assertEqual([s['overrides']['max_levels'] for s in updated['scenarios']], [60, 30, 15])
        self.log.write_text('')
        self.install('--compare')
        self.assertEqual(self.restarts(), [])

    def test_help_and_bad_arguments_do_not_change_the_system(self):
        help_result = self.install("--help")
        self.assertIn("0.5% / 1% / 2%", help_result.stdout)
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

    def test_cleanup_keeps_current_backup_and_older_engine_and_web(self):
        self.install()
        engine_revision = self.revision
        with (self.source / 'variational_grid/web/styles.css').open('a') as file:
            file.write('\n/* second web */\n')
        web_revision = self.commit()
        self.install()
        revisions = []
        for number in range(3):
            (self.source / 'README.md').write_text(f'Documentation {number}')
            revisions.append(self.commit())
            self.install()
        retained = {path.name for path in (self.app / 'releases').iterdir()}
        self.assertEqual(retained, {engine_revision, web_revision, *revisions[-2:]})
        unknown = self.app / 'releases' / ('f' * 40)
        unknown.mkdir()
        (unknown / 'keep').write_text('user content')
        stale = self.app / 'releases' / ('e' * 40)
        stale.mkdir()
        (stale / '.install-owned').write_text('variational-grid\n')
        orphan_stamp = self.app / 'validated' / ('f' * 64)
        orphan_stamp.write_text('unreferenced')
        before = {path: path.read_bytes() for path in self.conf.iterdir() if path.is_file()}
        self.log.write_text('')
        self.install('--cleanup')
        self.assertEqual(self.restarts(), [])
        self.assertFalse(any(call[0] in ('apt-get', 'git', 'runuser') for call in self.calls()))
        self.assertFalse(stale.exists())
        self.assertFalse(orphan_stamp.exists())
        self.assertEqual((unknown / 'keep').read_text(), 'user content')
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        with (self.source / 'variational_grid/engine.py').open('a') as file:
            file.write('\n# new runtime\n')
        newest = self.commit()
        self.install()
        self.assertEqual({path.name for path in (self.app / 'releases').iterdir()},
                         {newest, revisions[-1], unknown.name})

    def test_low_capacity_or_inodes_stop_before_fetch_validation_or_restart(self):
        self.install()
        original_release = (self.app / 'current').resolve()
        original_validation = self.validation_log.read_bytes()
        original_config = (self.conf / 'config.json').read_bytes()
        for kind, message in (('bytes', 'Insufficient disk space'), ('inodes', 'Insufficient inodes')):
            self.env['GRID_INSTALL_TEST_LOW_STORAGE'] = kind
            self.log.write_text('')
            result = self.install(expected=1)
            self.assertIn(message, result.stderr)
            self.assertEqual((self.app / 'current').resolve(), original_release)
            self.assertEqual(self.validation_log.read_bytes(), original_validation)
            self.assertEqual((self.conf / 'config.json').read_bytes(), original_config)
            self.assertEqual(self.restarts(), [])
            self.assertFalse(any(call[0] == 'git' and any(arg in ('fetch', 'clone', 'archive')
                                                        for arg in call[1:]) for call in self.calls()))

    def test_service_inspection_failure_preserves_all_releases(self):
        self.install()
        stale = self.app / 'releases' / ('e' * 40)
        stale.mkdir()
        (stale / '.install-owned').write_text('variational-grid\n')
        self.env['GRID_INSTALL_TEST_FAIL_INSPECTION'] = '1'
        self.log.write_text('')
        self.install('--cleanup', expected=1)
        self.assertTrue(stale.is_dir())
        self.assertEqual((self.app / 'current').resolve().name, self.revision)
        self.assertEqual(self.restarts(), [])

    def test_configuration_failure_discards_prepared_unactivated_release(self):
        self.install()
        config_path = self.conf / 'config.json'
        config = json.loads(config_path.read_text())
        config['poll_seconds'] = -1
        config_path.write_text(json.dumps(config))
        (self.source / 'README.md').write_text('New documentation')
        revision = self.commit()
        self.log.write_text('')
        self.install(expected=1)
        self.assertEqual((self.app / 'current').resolve().name, self.revision)
        self.assertFalse((self.app / 'releases' / revision).exists())
        self.assertEqual(self.restarts(), [])


if __name__ == "__main__":
    unittest.main()
