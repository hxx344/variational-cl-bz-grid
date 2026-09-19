import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from variational_grid.client import Client, save_session
from variational_grid.models import GridError
from variational_grid.protection import decode_session
from test_grid import token


@unittest.skipUnless(os.name == "nt", "Windows DPAPI contract")
class WindowsProtectionTests(unittest.TestCase):
    def test_persisted_bytes_contain_no_token_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            value = token()
            save_session(path, {"token": value})
            self.assertNotIn(value, path.read_text())
            self.assertNotIn('"token"', path.read_text())
            self.assertEqual(Client(path).session()[0], value)

    def test_plaintext_and_tampered_ciphertext_are_refused(self):
        with self.assertRaises(GridError):
            decode_session({"token": token()})
        with self.assertRaises(GridError):
            decode_session({"format": "windows-dpapi-v1", "protected": "dGFtcGVyZWQ="})

    def test_encrypt_failure_never_creates_plaintext_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with patch("variational_grid.protection._dpapi", side_effect=GridError("DPAPI failed")):
                with self.assertRaises(GridError):
                    save_session(path, {"token": token()})
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name("session.json.new").exists())
