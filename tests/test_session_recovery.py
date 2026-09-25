"""Credential replacement and high-frequency locks remain recoverable."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from variational_grid.cli import main
from variational_grid.client import Client, save_session
from variational_grid.models import GridError
from variational_grid.store import ProcessLock
from test_grid import token


class SessionRecoveryTests(unittest.TestCase):
    def test_failed_replace_preserves_session_cleans_owned_temp_and_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            save_session(path, {"token": token()})
            before = path.read_bytes()
            orphan = path.with_name(path.name + ".new")
            orphan.write_text("pre-existing; do not delete")
            with patch("variational_grid.client.os.replace", side_effect=OSError("injected")):
                with self.assertRaises(GridError):
                    save_session(path, {"token": token()})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("*.new")), [orphan])
            fresh = token(time.time() + 86400)
            save_session(path, {"token": fresh})
            self.assertEqual(Client(path).session()[0], fresh)
            self.assertTrue(orphan.exists())

    def test_cli_rejected_candidate_does_not_replace_working_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch("variational_grid.cli.getpass.getpass", return_value=token()), \
                 patch("variational_grid.cli.CandidateSession.check_session", side_effect=GridError("rejected")), \
                 patch("variational_grid.cli.save_session") as save, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["init-session", "--config", str(path)]), 2)
                save.assert_not_called()

    def test_cli_rechecks_expiry_after_verification_before_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("variational_grid.cli.getpass.getpass", return_value=token()), \
                 patch("variational_grid.cli.CandidateSession.check_session", return_value={"authenticated": True}), \
                 patch("variational_grid.cli.CandidateSession.session", side_effect=GridError("expired")), \
                 patch("variational_grid.cli.save_session") as save, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["init-session", "--config", str(Path(directory) / "config.json")]), 2)
                save.assert_not_called()

    def test_lock_stays_one_byte_and_contenders_do_not_touch_owner_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger"
            lock = Path(str(path) + ".lock")
            lock.write_bytes(b"0" * 1000)
            for _ in range(100):
                with ProcessLock(path):
                    self.assertEqual(lock.stat().st_size, 1)
                    with self.assertRaises(GridError):
                        with ProcessLock(path):
                            pass
                    self.assertEqual(lock.stat().st_size, 1)


if __name__ == "__main__":
    unittest.main()
