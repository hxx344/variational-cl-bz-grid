"""Linux CI checks the same inherited terminal used by the stack's setsid child."""
import os
import subprocess
import sys
import time
import unittest


@unittest.skipUnless(sys.platform == 'linux', 'Requires native Linux PTY')
class InstallerTerminalTests(unittest.TestCase):
    def test_getpass_in_detached_session_hides_input_and_never_logs_token(self):
        import pty
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        process = subprocess.Popen([sys.executable, '-c',
            'import getpass; value=getpass.getpass("vr-token (hidden): "); print("accepted" if value == "fixture-secret" else "failed")'],
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while termios.tcgetattr(slave)[3] & termios.ECHO and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(termios.tcgetattr(slave)[3] & termios.ECHO, 'getpass must disable echo before typing')
        os.write(master, b'fixture-secret\n')
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertIn(b'accepted', stdout)
        self.assertNotIn(b'fixture-secret', stdout + stderr)
        self.assertNotIn(b'Warning', stderr)
