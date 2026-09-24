"""Exercise the Windows launcher with a native SSH stand-in; never connect remotely."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell tunnel launcher")
class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="grid tunnel ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = Path(__file__).resolve().parents[1] / "scripts/dashboard-tunnel.ps1"
        self.calls = self.root / "calls.jsonl"
        self.plan = self.root / "plan.json"
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"],
                        TUNNEL_TEST_CALLS=str(self.calls), TUNNEL_TEST_PLAN=str(self.plan))
        (self.root / "ssh.cmd").write_text(f'@"{sys.executable}" "{self.root / "ssh_stub.py"}" %*\n@exit /b %errorlevel%\n')
        (self.root / "ssh_stub.py").write_text('''import json, os, sys
from pathlib import Path
calls = Path(os.environ['TUNNEL_TEST_CALLS'])
prior = calls.read_text().splitlines() if calls.exists() else []
args = sys.argv[1:]
with calls.open('a') as stream: stream.write(json.dumps(args) + '\\n')
step = json.loads(Path(os.environ['TUNNEL_TEST_PLAN']).read_text())[len(prior)]
Path(args[args.index('-E') + 1]).write_text(step.get('message', ''))
sys.exit(step['code'])
''')
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.shells = list(dict.fromkeys(filter(None, (shutil.which("powershell.exe"), shutil.which("pwsh.exe")))))
        self.assertTrue(self.shells)

    def run_tunnel(self, shell, plan, extra=(), expected=0):
        self.calls.unlink(missing_ok=True)
        self.plan.write_text(json.dumps(plan))
        result = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script),
                                 "-SshHost", "test-user@server.invalid", "-LocalPort", str(self.port),
                                 "-RetrySeconds", "1", *extra], env=self.env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        for args in calls:
            self.assertFalse(Path(args[args.index("-E") + 1]).exists(), "Diagnostic log must be removed")
        return result, calls

    def test_transport_reset_retries_with_backoff_and_same_forwarding(self):
        for shell in self.shells:
            with self.subTest(shell=shell):
                result, calls = self.run_tunnel(shell, [
                    {"code": 255, "message": "client_loop: send disconnect: Connection reset"},
                    {"code": 255, "message": "Connection closed by 192.0.2.1 port 22"}, {"code": 0}], ["-SshPort", "2222"])
                self.assertEqual(len(calls), 3)
                self.assertIn("Reconnecting in 1 seconds", result.stdout)
                self.assertIn("Reconnecting in 2 seconds", result.stdout)
                for args in calls:
                    self.assertEqual(args[args.index("-L") + 1], f"127.0.0.1:{self.port}:127.0.0.1:9876")
                    self.assertEqual(args[args.index("-p") + 1], "2222")
                    self.assertEqual(args[-1], "test-user@server.invalid")
                    for option in ("ServerAliveInterval=15", "ServerAliveCountMax=6", "ExitOnForwardFailure=yes", "ConnectTimeout=15", "ForkAfterAuthentication=no", "ControlPath=none"):
                        self.assertIn(option, args)
                    self.assertIn("-N", args)
                    self.assertIn("-T", args)
                    self.assertFalse(any(x in args for x in ("-n", "-f", "BatchMode=yes", "StrictHostKeyChecking=no")))

    def test_normal_exit_does_not_retry_or_override_ssh_config_port(self):
        for shell in self.shells:
            result, calls = self.run_tunnel(shell, [{"code": 0}])
            self.assertEqual(len(calls), 1)
            self.assertNotIn("-p", calls[0])
            self.assertNotIn("Reconnecting", result.stdout)

    def test_permanent_and_unknown_errors_do_not_retry(self):
        messages = ["Permission denied (publickey,password).", "Host key verification failed.",
                    "REMOTE HOST IDENTIFICATION HAS CHANGED!", "bind: Address already in use",
                    "Could not resolve hostname invalid: Name or service not known",
                    "Unrecognized SSH failure", "channel 0: open failed: connect failed: Connection refused\nunknown terminal failure"]
        for shell in self.shells:
            for message in messages:
                with self.subTest(shell=shell, message=message):
                    result, calls = self.run_tunnel(shell, [{"code": 255, "message": message}], expected=1)
                    self.assertEqual(len(calls), 1)
                    self.assertNotIn("Reconnecting", result.stdout)

    def test_occupied_port_stops_before_ssh_and_preserves_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.port))
            listener.listen()
            for shell in self.shells:
                result, calls = self.run_tunnel(shell, [], expected=1)
                self.assertEqual(calls, [])
                self.assertIn("Close the old tunnel yourself", result.stderr)
                self.assertEqual(listener.getsockname()[1], self.port)

    def test_cancelling_retry_pipeline_does_not_start_another_ssh(self):
        # Stopping the PowerShell pipeline models Ctrl+C during its retry wait.
        harness = self.root / "cancel.ps1"
        harness.write_text('''$ErrorActionPreference = 'Stop'
$pipeline = [PowerShell]::Create()
$null = $pipeline.AddCommand($env:TUNNEL_TEST_SCRIPT).AddParameter('SshHost', 'server.invalid').AddParameter('LocalPort', [int]$env:TUNNEL_TEST_PORT).AddParameter('RetrySeconds', 60)
$run = $pipeline.BeginInvoke()
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 50
        $waiting = @($pipeline.Streams.Information | Where-Object { $_.MessageData -like '*Reconnecting in*' }).Count -gt 0
    } while (-not $waiting -and [DateTime]::UtcNow -lt $deadline)
    if (-not $waiting) { throw 'Retry wait was not reached' }
    $pipeline.Stop()
    if ($pipeline.InvocationStateInfo.State -ne 'Stopped') { throw 'Pipeline did not stop' }
    Start-Sleep -Milliseconds 200
} finally { $pipeline.Dispose() }
''')
        for shell in self.shells:
            self.calls.unlink(missing_ok=True)
            self.plan.write_text(json.dumps([{"code": 255, "message": "client_loop: send disconnect: Connection reset"}]))
            env = dict(self.env, TUNNEL_TEST_SCRIPT=str(self.script), TUNNEL_TEST_PORT=str(self.port))
            result = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)], env=env, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
            self.assertEqual(len(calls), 1)
            self.assertFalse(Path(calls[0][calls[0].index("-E") + 1]).exists())


if __name__ == "__main__":
    unittest.main()
