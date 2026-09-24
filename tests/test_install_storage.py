"""Exercise the installer's actual storage helper with native Python, no services."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='grid-storage-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / 'app'
        self.conf = self.root / 'conf'
        self.state = self.root / 'state'
        self.proc = self.root / 'proc'
        self.mounts = self.root / 'mountinfo'
        self.mounts.write_text('')
        for path in (self.app / 'releases', self.app / 'validated', self.conf, self.state, self.proc):
            path.mkdir(parents=True)
        installer = (Path(__file__).resolve().parents[1] / 'install.sh').read_text(encoding='utf8')
        helper = installer.split("<<'STORAGE_PY'\n", 1)[1].split('\nSTORAGE_PY\n', 1)[0]
        helper = helper.replace("Path('/proc')", f'Path({str(self.proc)!r})')
        helper = helper.replace("Path('/proc/self/mountinfo')", f'Path({str(self.mounts)!r})')
        namespace = {'__name__': 'storage_under_test'}
        exec(compile(helper, 'install.sh:STORAGE_PY', 'exec'), namespace)
        self.storage_type = namespace['DeploymentStorage']
        self.manager = self.storage_type(self.app, self.conf, self.state)

    def release(self, number, *, ready=True, key=None):
        path = self.app / 'releases' / f'{number:040x}'
        path.mkdir()
        (path / '.install-owned').write_text('variational-grid\n')
        (path / '.install-validation').write_text(key or f'{number:064x}')
        if ready:
            stamp = path / '.install-ready'
            stamp.touch()
            os.utime(stamp, ns=(number * 1_000_000_000, number * 1_000_000_000))
        return path

    def prune(self, preferred=''):
        with contextlib.redirect_stdout(io.StringIO()):
            self.manager.prune(str(preferred))

    def symlink(self, path, target, *, directory=True):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            self.skipTest(f'Symlinks unavailable: {type(error).__name__}')

    def test_retains_current_backup_and_both_older_running_directories(self):
        releases = [self.release(i) for i in range(1, 7)]
        self.manager.current = releases[5]
        self.manager.running = [releases[0], releases[2]]
        self.prune(releases[4])
        self.assertEqual({p.name for p in (self.app / 'releases').iterdir()},
                         {releases[i].name for i in (0, 2, 4, 5)})
        self.manager.running = [releases[5], releases[5]]
        self.prune()
        self.assertEqual({p.name for p in (self.app / 'releases').iterdir()},
                         {releases[i].name for i in (4, 5)})

    def test_ready_backup_beats_newer_failed_candidate(self):
        backup, current = self.release(1), self.release(2)
        failed = self.release(3, ready=False)
        self.manager.current = current
        self.prune()
        self.assertTrue(backup.exists())
        self.assertTrue(current.exists())
        self.assertFalse(failed.exists())

    def test_unknown_directories_and_symlinks_are_untouched(self):
        unknown = self.app / 'releases' / ('f' * 40)
        unknown.mkdir()
        sentinel = self.root / 'external'
        sentinel.mkdir()
        (sentinel / 'keep').write_text('user content')
        link = self.app / 'releases' / ('e' * 40)
        self.symlink(link, sentinel)
        self.prune()
        self.assertTrue(unknown.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertEqual((sentinel / 'keep').read_text(), 'user content')

    def test_configured_data_and_source_symlink_protect_their_releases(self):
        data_release, source_release = self.release(1), self.release(2)
        current = self.release(3)
        self.release(4)
        (data_release / 'data').mkdir()
        (self.conf / 'config.json').write_text(json.dumps({'state_file': str(data_release / 'data/paper.sqlite3')}))
        self.symlink(self.app / 'source', source_release)
        self.manager = self.storage_type(self.app, self.conf, self.state)
        self.manager.current = current
        self.prune()
        self.assertTrue(data_release.exists())
        self.assertTrue(source_release.exists())

    def test_data_protection_includes_descendants_and_ancestors(self):
        release = self.release(1, ready=False)
        self.manager.protect(self.app / 'releases')
        self.assertFalse(self.manager.remove(release))
        self.manager.roots = []
        self.manager.protect(release / 'nested/session.json')
        self.assertFalse(self.manager.remove(release))

    def test_running_installer_source_is_kept_even_from_another_working_directory(self):
        release = self.release(1, ready=False)
        script = release / 'install.sh'
        script.touch()
        self.manager = self.storage_type(self.app, self.conf, self.state, script)
        self.assertFalse(self.manager.remove(release))

    def test_configured_data_path_protects_its_containing_release(self):
        release = self.release(1, ready=False)
        (self.conf / 'config.json').write_text(json.dumps({'state_file': str(release / 'paper.sqlite3')}))
        self.manager = self.storage_type(self.app, self.conf, self.state)
        self.assertFalse(self.manager.remove(release))

    def test_inventory_output_protects_its_release_for_absolute_and_config_relative_paths(self):
        release = self.release(1, ready=False)
        output = release / 'inventory-data'
        output.mkdir()
        sentinel = output / 'ledger.sqlite3'
        sentinel.write_bytes(b'preserved inventory ledger')
        for relative in (False, True):
            with self.subTest(relative=relative):
                configured = os.path.relpath(output, self.conf) if relative else str(output)
                (self.conf / 'inventory.json').write_text(json.dumps({'kind': 'inventory', 'output_dir': configured}))
                self.manager = self.storage_type(self.app, self.conf, self.state)
                self.assertFalse(self.manager.remove(release))
                self.assertEqual(sentinel.read_bytes(), b'preserved inventory ledger')

    def test_inventory_base_protects_its_session_path(self):
        release = self.release(1, ready=False)
        (self.conf / 'inventory-base.json').write_text(json.dumps({'session_file': str(release / 'session.json')}))
        self.manager = self.storage_type(self.app, self.conf, self.state)
        self.assertFalse(self.manager.remove(release))

    def test_reclaims_owned_staging_and_deployment_scratch_only(self):
        for root, name in ((self.app, '.deploy.ABC123'), (self.app / 'releases', '.staging.ABC123')):
            owned = root / name
            owned.mkdir()
            (owned / '.install-owned').write_text('variational-grid\n')
            unknown = root / name.replace('ABC123', 'DEF456')
            unknown.mkdir()
        self.prune()
        self.assertFalse((self.app / '.deploy.ABC123').exists())
        self.assertFalse((self.app / 'releases/.staging.ABC123').exists())
        self.assertTrue((self.app / '.deploy.DEF456').exists())
        self.assertTrue((self.app / 'releases/.staging.DEF456').exists())

    def test_removes_unreferenced_validation_and_keeps_shared_keys(self):
        shared_key = 'a' * 64
        self.release(1, key=shared_key)
        backup = self.release(2, key=shared_key)
        current = self.release(3)
        self.manager.current = current
        cache = self.app / 'validated'
        for key in (shared_key, f'{3:064x}', 'b' * 64, 'notes'):
            (cache / key).write_text(key)
        self.prune(backup)
        self.assertEqual({p.name for p in cache.iterdir()}, {shared_key, f'{3:064x}', 'notes'})

    def test_validation_directory_with_configured_data_is_preserved(self):
        stamp = self.app / 'validated' / ('a' * 64)
        stamp.write_text('user data')
        self.manager.protect(stamp)
        self.manager.prune_validation()
        self.assertEqual(stamp.read_text(), 'user data')

    def test_legacy_manifest_is_required_and_one_legacy_backup_is_kept(self):
        old, backup, current = [self.release(i) for i in range(1, 4)]
        for path in (old, backup):
            (path / '.install-owned').unlink()
            (path / '.install-ready').unlink()
            (path / 'pyproject.toml').write_text('[project]\nname="variational-cl-bz-grid"\n')
            (path / 'install.sh').touch()
            (path / 'variational_grid').mkdir()
            (path / 'variational_grid/__init__.py').touch()
        self.manager.current = current
        self.prune(backup)
        self.assertFalse(old.exists())
        self.assertTrue(backup.exists())

    def test_candidates_containing_mounts_are_preserved(self):
        release = self.release(1, ready=False)
        nested = release / 'data'
        nested.mkdir()
        for mount in (release, nested):
            with patch('os.path.ismount', side_effect=lambda p: Path(p) == mount):
                self.assertFalse(self.manager.remove(release))
        self.assertTrue(nested.exists())

    def test_linux_bind_mounts_on_same_device_are_preserved(self):
        release = self.release(1, ready=False)
        for mount in (release, release / 'data'):
            self.mounts.write_text(f'1 0 0:1 / {mount.as_posix()} rw - ext4 /dev/example rw\n')
            with patch('sys.platform', 'linux'), patch('os.path.ismount', return_value=False):
                self.assertFalse(self.manager.remove(release))

    def test_validation_cache_bind_mount_is_preserved(self):
        cache = self.app / 'validated'
        stamp = cache / ('a' * 64)
        stamp.write_text('mounted data')
        self.mounts.write_text(f'1 0 0:1 / {stamp.as_posix()} rw - ext4 /dev/example rw\n')
        with patch('sys.platform', 'linux'), patch('os.path.ismount', return_value=False):
            self.manager.prune_validation()
        self.assertEqual(stamp.read_text(), 'mounted data')

    def test_service_inspection_failure_and_missing_pid_stop_cleanup(self):
        for stdout, code in (('', 1), ('LoadState=loaded\n', 0),
                             ('LoadState=loaded\nMainPID=bad\n', 0)):
            result = subprocess.CompletedProcess([], code, stdout, '')
            with patch('subprocess.run', return_value=result):
                with self.assertRaises(RuntimeError):
                    self.manager.inspect_services()

    def test_running_process_directory_is_protected_and_missing_cwd_fails(self):
        release = self.release(1, ready=False)
        (self.proc / '123').mkdir()
        self.symlink(self.proc / '123/cwd', release)
        result = subprocess.CompletedProcess([], 0, 'LoadState=loaded\nMainPID=123\n', '')
        with patch('subprocess.run', return_value=result):
            self.manager.inspect_services()
            self.assertFalse(self.manager.remove(release))
            (self.proc / '123/cwd').unlink()
            with self.assertRaises(RuntimeError):
                self.manager.inspect_services()

    def test_invalid_configuration_does_not_allow_cleanup(self):
        release = self.release(1)
        (self.conf / 'config.json').write_text('{broken')
        with self.assertRaises(ValueError):
            self.storage_type(self.app, self.conf, self.state)
        self.assertTrue(release.exists())


if __name__ == '__main__':
    unittest.main()
