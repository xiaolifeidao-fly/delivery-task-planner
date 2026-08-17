#!/usr/bin/env python3

import importlib.util
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


CERT_SCRIPT_PATH = Path(__file__).resolve().parent / "ensure_https_cert.py"
SPEC = importlib.util.spec_from_file_location("delivery_task_https_cert", CERT_SCRIPT_PATH)
certificate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(certificate)


class EnsureHttpsCertificateTest(unittest.TestCase):
    def test_generates_a_private_ca_and_loopback_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory) / "runtime"
            ca_certificate, bridge_certificate, bridge_key = certificate.ensure_certificates(runtime_dir)

            self.assertTrue(ca_certificate.is_file())
            self.assertTrue(bridge_certificate.is_file())
            self.assertTrue(bridge_key.is_file())
            self.assertEqual(0o700, stat.S_IMODE((runtime_dir / "tls").stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(bridge_key.stat().st_mode))
            self.assertEqual(0o644, stat.S_IMODE(bridge_certificate.stat().st_mode))
            details = subprocess.run(
                ["openssl", "x509", "-in", str(bridge_certificate), "-noout", "-text"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("DNS:localhost", details)
            self.assertIn("IP Address:127.0.0.1", details)
            self.assertIn("IP Address:0:0:0:0:0:0:0:1", details)

    def test_reuses_existing_certificate_material(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory) / "runtime"
            expected = certificate.ensure_certificates(runtime_dir)

            with patch.object(certificate, "run_openssl") as run_openssl:
                actual = certificate.ensure_certificates(runtime_dir)

            self.assertEqual(expected, actual)
            run_openssl.assert_not_called()

    def test_windows_uses_powershell_and_requests_current_user_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory) / "runtime"

            def initialize(arguments, **_kwargs):
                ca_certificate, _ca_key, bridge_certificate, bridge_key = certificate.tls_paths(runtime_dir)
                ca_certificate.parent.mkdir(parents=True)
                for path in (ca_certificate, bridge_certificate, bridge_key):
                    path.write_text("generated", encoding="utf-8")
                return subprocess.CompletedProcess(arguments, 0)

            with (
                patch.object(certificate.sys, "platform", "win32"),
                patch.object(certificate.shutil, "which", side_effect=lambda command: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe" if command == "powershell.exe" else None),
                patch.object(certificate.subprocess, "run", side_effect=initialize) as run,
            ):
                result = certificate.ensure_certificates(runtime_dir, install_trust=True)

            self.assertEqual(certificate.tls_paths(runtime_dir)[::2], result[:2])
            self.assertIn("-InstallTrust", run.call_args.args[0])
            self.assertIn("-RuntimeDir", run.call_args.args[0])

    def test_windows_runtime_uses_local_app_data(self):
        with (
            patch.object(certificate.sys, "platform", "win32"),
            patch.dict(certificate.os.environ, {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}, clear=False),
        ):
            self.assertEqual(Path("C:/Users/test/AppData/Local/delivery-task-planner"), certificate.default_runtime_dir())

    def test_macos_trust_installs_the_ca_when_not_already_present(self):
        ca_certificate = Path("/tmp/ca.pem")
        missing = subprocess.CompletedProcess([], 1)
        installed = subprocess.CompletedProcess([], 0)
        with (
            patch.object(certificate.sys, "platform", "darwin"),
            patch.object(certificate.subprocess, "run", side_effect=[missing, installed]) as run,
        ):
            changed = certificate.install_macos_trust(ca_certificate)

        self.assertTrue(changed)
        self.assertEqual("security", run.call_args_list[0].args[0][0])
        self.assertEqual("add-trusted-cert", run.call_args_list[1].args[0][1])
        self.assertEqual(str(ca_certificate), run.call_args_list[1].args[0][-1])


if __name__ == "__main__":
    unittest.main()
