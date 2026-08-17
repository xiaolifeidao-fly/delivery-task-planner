#!/usr/bin/env python3
"""Create the loopback TLS certificate used by the delivery board bridge."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def default_runtime_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "delivery-task-planner"
    return Path.home() / ".local" / "state" / "delivery-task-planner"


RUNTIME_DIR = default_runtime_dir()
TLS_DIRECTORY_NAME = "tls"
CA_COMMON_NAME = "Universe Delivery Task Planner Local CA"
CA_CERTIFICATE_NAME = "ca.pem"
CA_KEY_NAME = "ca-key.pem"
BRIDGE_CERTIFICATE_NAME = "bridge-cert.pem"
BRIDGE_KEY_NAME = "bridge-key.pem"

BRIDGE_CERTIFICATE_CONFIG = """\
[req]
distinguished_name = subject
prompt = no

[subject]
CN = 127.0.0.1

[bridge_extensions]
basicConstraints = critical, CA:FALSE
extendedKeyUsage = serverAuth
keyUsage = critical, digitalSignature, keyEncipherment
subjectAltName = @subject_alt_names

[subject_alt_names]
DNS.1 = localhost
IP.1 = 127.0.0.1
IP.2 = ::1
"""


class CertificateError(RuntimeError):
    pass


def tls_paths(runtime_dir: Path) -> tuple[Path, Path, Path, Path]:
    directory = runtime_dir / TLS_DIRECTORY_NAME
    return (
        directory / CA_CERTIFICATE_NAME,
        directory / CA_KEY_NAME,
        directory / BRIDGE_CERTIFICATE_NAME,
        directory / BRIDGE_KEY_NAME,
    )


def run_openssl(*arguments: str) -> None:
    try:
        subprocess.run(
            ["openssl", *arguments],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CertificateError("未找到 openssl，无法创建本地 HTTPS 证书。") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise CertificateError(f"创建本地 HTTPS 证书失败：{detail or 'openssl 执行失败'}") from exc


def set_permissions(path: Path, mode: int) -> None:
    path.chmod(mode)


def ensure_windows_certificates(runtime_dir: Path, install_trust: bool) -> tuple[Path, Path, Path]:
    script = Path(__file__).with_name("ensure_https_cert.ps1")
    if not script.is_file():
        raise CertificateError("缺少 Windows HTTPS 证书初始化脚本。")
    command = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not command:
        raise CertificateError("未找到 PowerShell，无法创建 Windows 本地 HTTPS 证书。")
    arguments = [
        command,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-RuntimeDir",
        str(runtime_dir),
    ]
    if install_trust:
        arguments.append("-InstallTrust")
    try:
        subprocess.run(arguments, check=True)
    except subprocess.CalledProcessError as exc:
        raise CertificateError("创建或信任 Windows 本地 HTTPS 证书失败。") from exc
    ca_certificate, _ca_key, bridge_certificate, bridge_key = tls_paths(runtime_dir)
    if not (ca_certificate.is_file() and bridge_certificate.is_file() and bridge_key.is_file()):
        raise CertificateError("Windows 证书初始化未生成桥接所需文件。")
    return ca_certificate, bridge_certificate, bridge_key


def ensure_certificates(runtime_dir: Path = RUNTIME_DIR, install_trust: bool = False) -> tuple[Path, Path, Path]:
    """Return CA, bridge certificate, and bridge private-key paths."""
    if sys.platform == "win32":
        return ensure_windows_certificates(runtime_dir, install_trust)
    tls_directory = runtime_dir / TLS_DIRECTORY_NAME
    tls_directory.mkdir(parents=True, exist_ok=True)
    set_permissions(tls_directory, 0o700)
    ca_certificate, ca_key, bridge_certificate, bridge_key = tls_paths(runtime_dir)

    if not (ca_certificate.is_file() and ca_key.is_file()):
        for path in (ca_certificate, ca_key):
            if path.exists():
                path.unlink()
        run_openssl(
            "req",
            "-x509",
            "-new",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_certificate),
            "-sha256",
            "-days",
            "3650",
            "-subj",
            f"/CN={CA_COMMON_NAME}",
        )

    if not (bridge_certificate.is_file() and bridge_key.is_file()):
        for path in (bridge_certificate, bridge_key):
            if path.exists():
                path.unlink()
        certificate_config = tls_directory / "bridge-openssl.cnf"
        certificate_request = tls_directory / "bridge-cert.csr"
        serial_file = tls_directory / "ca.srl"
        certificate_config.write_text(BRIDGE_CERTIFICATE_CONFIG, encoding="utf-8")
        try:
            run_openssl(
                "req",
                "-new",
                "-nodes",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(bridge_key),
                "-out",
                str(certificate_request),
                "-config",
                str(certificate_config),
            )
            run_openssl(
                "x509",
                "-req",
                "-in",
                str(certificate_request),
                "-CA",
                str(ca_certificate),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(bridge_certificate),
                "-days",
                "825",
                "-sha256",
                "-extfile",
                str(certificate_config),
                "-extensions",
                "bridge_extensions",
            )
        finally:
            for path in (certificate_config, certificate_request, serial_file):
                if path.exists():
                    path.unlink()

    for path in (ca_key, bridge_key):
        set_permissions(path, 0o600)
    for path in (ca_certificate, bridge_certificate):
        set_permissions(path, 0o644)
    return ca_certificate, bridge_certificate, bridge_key


def install_macos_trust(ca_certificate: Path) -> bool:
    """Trust the private root in the current user's login keychain once."""
    if sys.platform != "darwin":
        return False
    keychain = Path.home() / "Library" / "Keychains" / "login.keychain-db"
    existing = subprocess.run(
        ["security", "find-certificate", "-c", CA_COMMON_NAME, str(keychain)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if existing.returncode == 0:
        return False
    try:
        subprocess.run(
            ["security", "add-trusted-cert", "-d", "-r", "trustRoot", "-k", str(keychain), str(ca_certificate)],
            check=True,
        )
    except FileNotFoundError as exc:
        raise CertificateError("未找到 macOS security 工具，无法将本地根证书加入钥匙串。") from exc
    except subprocess.CalledProcessError as exc:
        raise CertificateError("无法将本地根证书加入登录钥匙串。") from exc
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the local HTTPS certificate for the delivery board bridge.")
    parser.add_argument("--runtime-dir", default=str(RUNTIME_DIR))
    parser.add_argument("--install-trust", action="store_true", help="Trust the local CA in the macOS login keychain.")
    args = parser.parse_args()
    try:
        ca_certificate, bridge_certificate, bridge_key = ensure_certificates(Path(args.runtime_dir).expanduser(), args.install_trust)
        trusted = install_macos_trust(ca_certificate) if args.install_trust and sys.platform == "darwin" else False
    except CertificateError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"HTTPS bridge certificate: {bridge_certificate}")
    print(f"HTTPS bridge private key: {bridge_key}")
    if trusted:
        print("The local bridge CA was added to the macOS login keychain.")


if __name__ == "__main__":
    main()
