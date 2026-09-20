"""Manage a Hive IPP Bridge user installation.

Developed by Edge Case Software — https://edgecasesoftware.dev
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import enrollment, ipp_command, submit
from .config import APPLICATION, IPP_PORT, LINUX_DEVICE_URI, LINUX_QUEUE_NAME


QUEUE_NAME = LINUX_QUEUE_NAME
LEGACY_QUEUE_NAMES = ("PaperCut_Hive",)
LEGACY_SERVICE_NAMES = ("papercut-hive-printer.service",)
DEVICE_URI = LINUX_DEVICE_URI
INSTALL_DIR = Path.home() / ".local/lib/hive-ipp-bridge"
LAUNCHER = Path.home() / ".local/bin/hive-ipp-bridge"
SERVICE_NAME = "hive-ipp-bridge.service"
CREDENTIALS = {
    "jwt": "PAPERCUT_HIVE_JWT",
    "client-id": "PAPERCUT_HIVE_CLIENT_ID",
    "org-id": "PAPERCUT_HIVE_ORG_ID",
}


def run(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, input=input_text, capture_output=True, text=True, check=False)


def require_commands(names: tuple[str, ...]) -> bool:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        print(f"error: required command(s) not found: {', '.join(missing)}", file=sys.stderr)
        return False
    return True


def secret_lookup(key: str) -> str | None:
    result = run(["secret-tool", "lookup", "application", APPLICATION, "key", key])
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def secret_store(key: str, value: str) -> bool:
    result = run(
        ["secret-tool", "store", "--label", "Hive IPP Bridge", "application", APPLICATION,
         "key", key], input_text=value
    )
    if result.returncode:
        print(f"error: could not store {key} in the desktop keyring", file=sys.stderr)
        print(
            "Ensure a Secret Service-compatible keyring is installed, unlocked, "
            "and available on the user D-Bus session.",
            file=sys.stderr,
        )
        return False
    return True


def secret_clear(key: str) -> None:
    run(["secret-tool", "clear", "application", APPLICATION, "key", key])


def enroll_credentials() -> bool:
    environment = {key: os.environ.get(name) for key, name in CREDENTIALS.items()}
    supplied = [key for key, value in environment.items() if value]
    if supplied:
        if len(supplied) != len(CREDENTIALS):
            print("error: the prototype credential variables must all be set together", file=sys.stderr)
            return False
        values = environment
    else:
        try:
            print("See 'Obtaining the PaperCut Hive setup link' in README.md.")
            setup_link = input("Paste the PaperCut Hive setup link: ")
            claimed = enrollment.claim(setup_link)
        except enrollment.EnrollmentError as error:
            print(f"error: {error}", file=sys.stderr)
            return False
        values = {"jwt": claimed.jwt, "client-id": claimed.client_id,
                  "org-id": claimed.organization_id}

    stored: list[str] = []
    for key in ("client-id", "org-id", "jwt"):
        value = values[key]
        if not value or not secret_store(key, value):
            for stored_key in stored:
                secret_clear(stored_key)
            return False
        stored.append(key)
    return True


def service_text() -> str:
    adapter = str(Path(ipp_command.__file__).resolve()).replace("\\", "\\\\").replace('"', '\\"')
    return f"""[Unit]
Description=Hive IPP Bridge user printer
After=graphical-session.target

[Service]
ExecStart=/usr/bin/ippeveprinter -n localhost -r off -2 -s 10,10 -f application/pdf -F application/pdf -p 8631 -c \"{adapter}\" \"Hive IPP Bridge\"
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""

class CupsError(RuntimeError):
    """The CUPS scheduler could not be queried."""


def queue_uri(queue_name: str = QUEUE_NAME) -> str | None:
    result = run(["lpstat", "-v"])
    message = (result.stderr or result.stdout).strip()
    if result.returncode:
        if message == "lpstat: No destinations added.":
            return None
        raise CupsError(message or "the CUPS scheduler is unavailable")
    prefix = f"device for {queue_name}: "
    return next((line.removeprefix(prefix) for line in result.stdout.splitlines()
                 if line.startswith(prefix)), None)


def wait_for_printer(uri: str = DEVICE_URI, timeout: float = 5.0) -> bool:
    parsed = urlsplit(uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or IPP_PORT
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def setup_command(_args: argparse.Namespace) -> int:
    if not require_commands(("secret-tool", "systemctl", "lpadmin", "lpstat", "ippeveprinter")):
        return 1
    try:
        existing_uri = queue_uri()
    except CupsError as error:
        print(f"error: unable to inspect CUPS: {error}", file=sys.stderr)
        return 1
    if existing_uri and existing_uri != DEVICE_URI:
        print(f"error: {QUEUE_NAME} points to {existing_uri}; leaving it untouched", file=sys.stderr)
        return 1

    for legacy_service in LEGACY_SERVICE_NAMES:
        legacy_path = Path.home() / ".config/systemd/user" / legacy_service
        if legacy_path.exists():
            if shutil.which("systemctl"):
                run(["systemctl", "--user", "disable", "--now", legacy_service])
            legacy_path.unlink()

    service_path = Path.home() / ".config/systemd/user" / SERVICE_NAME
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service_text(), encoding="utf-8")
    for command in (["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", SERVICE_NAME]):
        result = run(command)
        if result.returncode:
            print(result.stderr.strip() or f"error: {' '.join(command)} failed", file=sys.stderr)
            return 1

    if not wait_for_printer():
        print("error: timed out waiting for the local IPP printer service to start", file=sys.stderr)
        return 1

    if not existing_uri:
        result = run(["lpadmin", "-p", QUEUE_NAME, "-E", "-v", DEVICE_URI, "-m", "everywhere"])
        if result.returncode:
            print(result.stderr.strip() or "error: could not create the CUPS queue", file=sys.stderr)
            return 1

    if not all(secret_lookup(key) for key in CREDENTIALS) and not enroll_credentials():
        return 1

    print("Hive IPP Bridge is set up and ready.")
    return 0


def status_command(_args: argparse.Namespace) -> int:
    credential_state = (all(secret_lookup(key) for key in CREDENTIALS)
                        if shutil.which("secret-tool") else False)
    service = (run(["systemctl", "--user", "is-active", SERVICE_NAME]).returncode == 0
               if shutil.which("systemctl") else False)
    try:
        uri = queue_uri() if shutil.which("lpstat") else None
    except CupsError as error:
        print(f"CUPS error: {error}", file=sys.stderr)
        uri = None
    states = (("Credentials", credential_state), ("User service", service),
              ("CUPS queue", uri == DEVICE_URI))
    for label, ready in states:
        print(f"{label}: {'ready' if ready else 'not ready'}")
    if uri and uri != DEVICE_URI:
        print(f"Queue URI: {uri}")
    return 0 if all(ready for _, ready in states) else 1


def test_command(args: argparse.Namespace) -> int:
    if not require_commands(("secret-tool",)):
        return 1
    credentials = {name: secret_lookup(key) for key, name in CREDENTIALS.items()}
    if any(value is None for value in credentials.values()):
        print("error: setup is incomplete; run 'hive-ipp-bridge setup'", file=sys.stderr)
        return 1
    old_environment = {name: os.environ.get(name) for name in credentials}
    try:
        os.environ.update({name: value for name, value in credentials.items() if value})
        return submit.main([str(args.document)])
    finally:
        for name, value in old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value



def remove_user_program() -> bool:
    try:
        launcher_text = LAUNCHER.read_text(encoding="utf-8")
    except FileNotFoundError:
        launcher_text = None
    except OSError as error:
        print(f"error: could not inspect {LAUNCHER}: {error}", file=sys.stderr)
        return False
    if launcher_text is not None and "HIVE_IPP_BRIDGE_LAUNCHER=1" not in launcher_text:
        print(f"error: {LAUNCHER} is not managed by Hive IPP Bridge; leaving it untouched", file=sys.stderr)
        return False
    try:
        if INSTALL_DIR.is_dir():
            shutil.rmtree(INSTALL_DIR)
        if launcher_text is not None:
            LAUNCHER.unlink()
    except OSError as error:
        print(f"error: could not remove the installed program: {error}", file=sys.stderr)
        return False
    return True


def uninstall_command(args: argparse.Namespace) -> int:
    if not require_commands(("lpstat", "lpadmin")):
        return 1
    for queue_name in (QUEUE_NAME, *LEGACY_QUEUE_NAMES):
        try:
            uri = queue_uri(queue_name)
        except CupsError as error:
            print(f"error: unable to inspect CUPS: {error}", file=sys.stderr)
            return 1
        if uri and uri != DEVICE_URI:
            print(f"error: {queue_name} points to {uri}; leaving it untouched", file=sys.stderr)
            return 1
        if uri and run(["lpadmin", "-x", queue_name]).returncode:
            print(f"error: could not remove CUPS queue {queue_name}", file=sys.stderr)
            return 1
    for service_name in (SERVICE_NAME, *LEGACY_SERVICE_NAMES):
        service_path = Path.home() / ".config/systemd/user" / service_name
        if shutil.which("systemctl"):
            run(["systemctl", "--user", "disable", "--now", service_name])
        if service_path.exists():
            service_path.unlink()
    if shutil.which("systemctl"):
        run(["systemctl", "--user", "daemon-reload"])
    purge = args.purge or args.full
    if purge:
        if not require_commands(("secret-tool",)):
            return 1
        for key in CREDENTIALS:
            run(["secret-tool", "clear", "application", APPLICATION, "key", key])
    if args.full:
        if not remove_user_program():
            return 1
        print("Hive IPP Bridge was fully removed, including credentials and the installed command.")
    elif purge:
        print("Hive IPP Bridge was uninstalled and its credentials were removed.")
    else:
        print("Hive IPP Bridge was uninstalled. Credentials were preserved; use --purge to remove them.")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hive-ipp-bridge")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="configure credentials, service, and queue").set_defaults(handler=setup_command)
    commands.add_parser("status", help="show setup health").set_defaults(handler=status_command)
    test = commands.add_parser("test", help="submit a PDF test document")
    test.add_argument("document", type=Path)
    test.set_defaults(handler=test_command)
    uninstall = commands.add_parser("uninstall", help="remove per-user configuration")
    uninstall.add_argument("--purge", action="store_true", help="also remove credentials")
    uninstall.add_argument("--full", action="store_true", help="remove credentials and installed program")
    uninstall.set_defaults(handler=uninstall_command)
    return root


def main() -> int:
    if sys.platform == "win32":
        from .windows.platform_cli import main as windows_main

        return windows_main()
    args = parser().parse_args()
    return args.handler(args)
