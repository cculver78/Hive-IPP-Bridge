"""Windows command-line lifecycle implementation."""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .. import enrollment
from ..config import (
    IPP_HOST,
    IPP_PORT,
    WINDOWS_PROVISIONER_PIPE_NAME,
    WINDOWS_PROVISIONER_SERVICE_NAME,
    WINDOWS_SERVICE_NAME,
    windows_user_ipp_url,
)
from . import printer, service
from .credentials import CredentialError, CredentialVault, clear_owner_sid, current_user_sid, sid_text
from .ipc import ControlClient, IpcError, request_with_document
from .paths import program_data_root


def wait_for_listener(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((IPP_HOST, IPP_PORT), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _source_command(extra: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *extra]
    return [sys.executable, "-m", "hive_ipp_bridge", *extra]


def _powershell_literal(value: str) -> str:
    """Return a PowerShell single-quoted string literal."""

    return "'" + value.replace("'", "''") + "'"


def _admin_operation(operation: str) -> int:
    try:
        if operation == "install-services":
            service.install_or_update()
            service.restart_service()
            printer.remove_legacy_single_user_printer()
        elif operation == "remove-services":
            service.remove_service()
        elif operation == "remove-all-printers":
            printer.remove_all_managed_printers()
        elif operation == "purge-vault":
            CredentialVault().clear()
            clear_owner_sid()
        elif operation == "purge-data":
            root = program_data_root()
            if root.name != "Hive IPP Bridge":
                raise OSError("refusing to remove an unexpected data directory")
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"error: unknown administrative operation {operation}", file=sys.stderr)
            return 2
    except (CredentialError, service.ServiceError, printer.PrinterError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # pragma: no cover - Windows API-specific failures
        print(f"error: administrative operation failed: {error}", file=sys.stderr)
        return 1
    return 0


def run_admin_operation(operation: str) -> int:
    if service.is_admin():
        return _admin_operation(operation)
    if os.name != "nt":
        return 1
    command = _source_command(["--admin-operation", operation])
    powershell_script = (
        f"$filePath = {_powershell_literal(command[0])}; "
        f"$argumentList = @({', '.join(_powershell_literal(value) for value in command[1:])}); "
        "$p = Start-Process -FilePath $filePath -ArgumentList $argumentList "
        "-Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    )
    encoded_script = base64.b64encode(powershell_script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
        check=False,
    )
    if result.returncode:
        print(
            f"error: elevated administrative operation '{operation}' failed "
            f"(exit code {result.returncode})",
            file=sys.stderr,
        )
    return result.returncode


def _credentials_from_environment() -> enrollment.Credentials | None:
    values = (
        os.environ.get("PAPERCUT_HIVE_JWT"),
        os.environ.get("PAPERCUT_HIVE_CLIENT_ID"),
        os.environ.get("PAPERCUT_HIVE_ORG_ID"),
    )
    if any(values) and not all(values):
        print("error: the prototype credential variables must all be set together", file=sys.stderr)
        return None
    if all(values):
        return enrollment.Credentials(values[0] or "", values[1] or "", values[2] or "")
    return None


def _enroll() -> enrollment.Credentials | None:
    environment_credentials = _credentials_from_environment()
    if environment_credentials:
        return environment_credentials
    try:
        print("See 'Obtaining the PaperCut Hive setup link' in README.md.")
        setup_link = input("Paste the PaperCut Hive setup link: ")
        return enrollment.claim(setup_link)
    except enrollment.EnrollmentError as error:
        print(f"error: {error}", file=sys.stderr)
        return None


def _current_username() -> str:
    username = os.environ.get("USERNAME", "User")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{username}" if domain else username


def _current_sid_text() -> str | None:
    sid = current_user_sid()
    return sid_text(sid) if sid is not None else None


def _store_credentials(credentials: enrollment.Credentials) -> dict[str, object] | None:
    try:
        response = ControlClient().request(
            {
                "op": "store",
                "jwt": credentials.jwt,
                "client_id": credentials.client_id,
                "org_id": credentials.organization_id,
                "username": _current_username(),
            }
        )
    except IpcError as error:
        print(f"error: could not store credentials in the Windows service: {error}", file=sys.stderr)
        return None
    if not response.get("ok"):
        print(f"error: {response.get('error', 'Windows service rejected credentials')}", file=sys.stderr)
        return None
    return response


def install_command(_args: argparse.Namespace) -> int:
    if os.name != "nt":
        print("error: Windows installation was requested on a non-Windows platform", file=sys.stderr)
        return 1
    if run_admin_operation("install-services"):
        return 1
    print("Hive IPP Bridge machine services are installed. Each user can now run setup without UAC.")
    return 0


def setup_command(_args: argparse.Namespace) -> int:
    if os.name != "nt":
        print("error: Windows setup was requested on a non-Windows platform", file=sys.stderr)
        return 1
    if not service.service_managed() or not service.service_managed(
        name=WINDOWS_PROVISIONER_SERVICE_NAME, provisioner=True
    ):
        print("error: an administrator must run 'HiveIPPBridge.exe install' first", file=sys.stderr)
        return 1
    if not wait_for_listener():
        print(f"error: timed out waiting for local IPP listener on port {IPP_PORT}", file=sys.stderr)
        return 1

    try:
        status = ControlClient().request({"op": "status"})
    except IpcError as error:
        print(f"error: Windows service is not ready: {error}", file=sys.stderr)
        return 1
    if not status.get("ok"):
        print(
            f"error: Windows service is not ready: "
            f"{status.get('error', 'control request was rejected')}",
            file=sys.stderr,
        )
        return 1
    if status.get("credentials") != "usable":
        credentials = _enroll()
        stored = _store_credentials(credentials) if credentials is not None else None
        if stored is None:
            return 1
        status.update(stored)
        status["credentials"] = "usable"
        status["username"] = _current_username()
    route_token = status.get("route_token")
    if not isinstance(route_token, str) or not route_token:
        print("error: Windows service did not return a private print route", file=sys.stderr)
        return 1
    username = _current_username()
    try:
        provisioned = ControlClient(WINDOWS_PROVISIONER_PIPE_NAME).request(
            {"op": "ensure-printer", "route_token": route_token, "username": username}
        )
    except IpcError as error:
        print(f"error: printer provisioner is unavailable: {error}", file=sys.stderr)
        return 1
    if not provisioned.get("ok"):
        print(f"error: {provisioned.get('error', 'printer provisioning failed')}", file=sys.stderr)
        return 1
    print(f"Hive IPP Bridge is set up for {_current_username()}.")
    return 0


def status_command(_args: argparse.Namespace) -> int:
    installed = service.service_installed()
    managed = service.service_managed() if installed else False
    state = service.service_state() if managed else ("not managed" if installed else "not installed")
    vault_present: bool | None = None
    credentials = "not ready"
    listener = False
    response: dict[str, object] = {}
    if state == "running":
        try:
            response = ControlClient().request({"op": "status"})
            credentials = "ready" if response.get("credentials") == "usable" else "not ready"
            vault_present = bool(response.get("vault"))
        except IpcError:
            credentials = "service unavailable"
        listener = wait_for_listener(1.0)
    if vault_present is None and state == "not installed":
        vault_present = CredentialVault().exists()
    provisioner_installed = service.service_installed(WINDOWS_PROVISIONER_SERVICE_NAME)
    provisioner_managed = service.service_managed(
        name=WINDOWS_PROVISIONER_SERVICE_NAME, provisioner=True
    ) if provisioner_installed else False
    provisioner_state = service.service_state(WINDOWS_PROVISIONER_SERVICE_NAME) if provisioner_managed else (
        "not managed" if provisioner_installed else "not installed"
    )
    current_sid = _current_sid_text()
    route_token = response.get("route_token")
    username = _current_username()
    expected_url = windows_user_ipp_url(route_token) if isinstance(route_token, str) and route_token else ""
    expected_name = printer.user_printer_name(username, current_sid) if current_sid else ""
    try:
        info = printer.inspect_printer(expected_name) if expected_name else None
        printer_state = "ready" if info and info.matches_profile(expected_name, expected_url) else "not ready"
    except printer.PrinterError as error:
        info = None
        printer_state = f"unavailable ({error})"
    states = [
        ("Operating system", "Windows"),
        ("Credentials", credentials + (" (vault present)" if vault_present else " (presence unknown)" if vault_present is None else "")),
        ("Windows service", state),
        ("Printer provisioner", provisioner_state),
        ("Local IPP listener", "ready" if listener else "not ready"),
        ("Expected port", str(IPP_PORT)),
        ("Print route", "private per-user localhost route" if expected_url else "not assigned"),
        ("Windows printer", printer_state),
        ("Printer driver", info.driver_name if info else "not detected"),
    ]
    for label, value in states:
        print(f"{label}: {value}")
    last_job = response.get("last_job")
    if isinstance(last_job, dict):
        print(
            "Last print job: "
            f"{last_job.get('state', 'unknown')} "
            f"({last_job.get('bytes', 0)} bytes) - "
            f"{last_job.get('message', 'no result')}"
        )
    return 0 if (
        credentials == "ready"
        and state == "running"
        and provisioner_state == "running"
        and listener
        and info
        and info.matches_profile(expected_name, expected_url)
    ) else 1


def test_command(args: argparse.Namespace) -> int:
    document = Path(args.document)
    if not document.is_file():
        print(f"error: file not found: {document}", file=sys.stderr)
        return 2
    if document.suffix.lower() != ".pdf":
        print("error: PaperCut Hive expects an application/pdf document", file=sys.stderr)
        return 2
    print(f"File: found ({document})")
    try:
        response = ControlClient().request(
            request_with_document(document.read_bytes(), op="test", title=document.stem)
        )
    except (IpcError, OSError) as error:
        print(f"Credentials/submission: unavailable ({error})", file=sys.stderr)
        return 1
    print(f"Credentials: {'usable' if response.get('ok') or response.get('accepted') else 'not usable'}")
    print("Submission: attempted")
    if response.get("accepted"):
        print("Hive: accepted the print job")
        return 0
    print(f"Hive: rejected the print job ({response.get('message', response.get('error', 'unknown error'))})", file=sys.stderr)
    return 1


def _schedule_full_cleanup() -> None:
    if not getattr(sys, "frozen", False):
        return
    root = Path(sys.executable).resolve().parent
    if not (root / "HiveIPPBridge.exe").is_file():
        return
    script = (
        "Start-Sleep -Seconds 2; "
        "Remove-Item -LiteralPath $args[0] -Recurse -Force -ErrorAction SilentlyContinue"
    )
    subprocess.Popen(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script, str(root)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )


def uninstall_command(args: argparse.Namespace) -> int:
    if getattr(args, "machine", False) or args.full:
        if run_admin_operation("remove-all-printers"):
            return 1
        if run_admin_operation("remove-services"):
            return 1
        if args.purge or args.full:
            if run_admin_operation("purge-vault"):
                return 1
        if args.full and run_admin_operation("purge-data"):
            return 1
        if args.full:
            _schedule_full_cleanup()
            print("Hive IPP Bridge was fully removed for all users.")
        else:
            print("Hive IPP Bridge machine services and managed user printers were removed.")
        return 0

    try:
        status = ControlClient().request({"op": "status"})
        route_token = status.get("route_token")
        if isinstance(route_token, str) and route_token:
            response = ControlClient(WINDOWS_PROVISIONER_PIPE_NAME).request(
                {"op": "remove-printer", "route_token": route_token, "username": _current_username()}
            )
            if not response.get("ok"):
                print(f"error: {response.get('error', 'printer removal failed')}", file=sys.stderr)
                return 1
        if args.purge:
            response = ControlClient().request({"op": "clear"})
            if not response.get("ok"):
                print(f"error: {response.get('error', 'credential removal failed')}", file=sys.stderr)
                return 1
    except IpcError as error:
        print(f"error: Windows service is unavailable: {error}", file=sys.stderr)
        return 1
    print(
        "Hive IPP Bridge was removed for the current user"
        + (" including credentials." if args.purge else "; credentials were preserved.")
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hive-ipp-bridge")
    root.add_argument(
        "--admin-operation",
        choices=("install-services", "remove-services", "remove-all-printers", "purge-vault", "purge-data"),
        help=argparse.SUPPRESS,
    )
    commands = root.add_subparsers(dest="command", required=False)
    commands.add_parser("install", help="install machine services as administrator").set_defaults(handler=install_command)
    commands.add_parser("setup", help="enroll the current user and provision their printer").set_defaults(handler=setup_command)
    commands.add_parser("status", help="show Windows setup health").set_defaults(handler=status_command)
    test = commands.add_parser("test", help="submit a PDF test document")
    test.add_argument("document", type=Path)
    test.set_defaults(handler=test_command)
    uninstall = commands.add_parser("uninstall", help="remove Windows configuration")
    uninstall.add_argument("--purge", action="store_true", help="also remove credentials")
    uninstall.add_argument("--full", action="store_true", help="remove credentials and installed program")
    uninstall.add_argument("--machine", action="store_true", help="remove machine services and all managed user printers")
    uninstall.set_defaults(handler=uninstall_command)
    return root


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--service":
        from .service import run_service_process

        run_service_process()
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--provisioner-service":
        from .service import run_provisioner_service_process

        run_provisioner_service_process()
        return 0
    args = parser().parse_args()
    if args.admin_operation:
        return _admin_operation(args.admin_operation)
    if not getattr(args, "handler", None):
        parser().error("a command is required")
    return args.handler(args)
