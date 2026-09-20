"""Windows service lifecycle and service-owned bridge runtime."""

from __future__ import annotations

import base64
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..config import (
    WINDOWS_PROVISIONER_PIPE_NAME,
    WINDOWS_PROVISIONER_SERVICE_DISPLAY_NAME,
    WINDOWS_PROVISIONER_SERVICE_NAME,
    WINDOWS_SERVICE_DISPLAY_NAME,
    WINDOWS_SERVICE_NAME,
)
from ..enrollment import Credentials
from ..submit import submit_pdf
from .credentials import CredentialError, CredentialVault, load_user_route
from .ipp_server import IppServer
from .ipc import ControlServer
from .logging import get_logger
from .paths import spool_dir
from . import printer


class ServiceError(RuntimeError):
    """A safe Windows service-management failure."""


def _win32_error_code(error: Exception) -> int | None:
    code = getattr(error, "winerror", None)
    if code is not None:
        return int(code)
    args = getattr(error, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _win32_modules():  # pragma: no cover - imported only on Windows
    try:
        import pywintypes
        import servicemanager
        import win32event
        import win32service
        import win32serviceutil
    except ImportError as error:
        raise ServiceError("pywin32 is required for Windows service support") from error
    return pywintypes, servicemanager, win32event, win32service, win32serviceutil


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def executable_path() -> Path:
    return Path(sys.executable).resolve()


def service_command(exe_path: Path | None = None, *, provisioner: bool = False) -> str:
    path = exe_path or executable_path()
    mode = "--provisioner-service" if provisioner else "--service"
    if getattr(sys, "frozen", False):
        return f'"{path}" {mode}'
    return f'"{path}" -m hive_ipp_bridge {mode}'


def service_installed(name: str = WINDOWS_SERVICE_NAME) -> bool:
    if os.name != "nt":
        return False
    _, _, _, win32service, _ = _win32_modules()
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        try:
            handle = win32service.OpenService(scm, name, win32service.SERVICE_QUERY_STATUS)
        except Exception:
            return False
        win32service.CloseServiceHandle(handle)
        return True
    finally:
        win32service.CloseServiceHandle(scm)


def service_managed(
    exe_path: Path | None = None,
    *,
    name: str = WINDOWS_SERVICE_NAME,
    provisioner: bool = False,
) -> bool:
    """Return whether the named service points at this application's executable."""

    if os.name != "nt" or not service_installed(name):
        return False
    _, _, _, win32service, _ = _win32_modules()
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        handle = win32service.OpenService(scm, name, win32service.SERVICE_QUERY_CONFIG)
        try:
            configured = str(win32service.QueryServiceConfig(handle)[3]).strip().lower()
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(scm)
    return configured == service_command(exe_path, provisioner=provisioner).lower()


def service_state(name: str = WINDOWS_SERVICE_NAME) -> str:
    if not service_installed(name):
        return "not installed"
    _, _, _, win32service, _ = _win32_modules()
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        handle = win32service.OpenService(scm, name, win32service.SERVICE_QUERY_STATUS)
        try:
            state = win32service.QueryServiceStatus(handle)[1]
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(scm)
    return {
        win32service.SERVICE_STOPPED: "stopped",
        win32service.SERVICE_START_PENDING: "starting",
        win32service.SERVICE_STOP_PENDING: "stopping",
        win32service.SERVICE_RUNNING: "running",
        win32service.SERVICE_CONTINUE_PENDING: "continuing",
        win32service.SERVICE_PAUSE_PENDING: "pausing",
        win32service.SERVICE_PAUSED: "paused",
    }.get(state, f"state {state}")


def install_or_update(exe_path: Path | None = None) -> None:
    if os.name != "nt":
        raise ServiceError("Windows services are available only on Windows")
    if not is_admin():
        raise ServiceError("administrator rights are required to install the Windows service")
    _, _, _, win32service, _ = _win32_modules()
    from .credentials import ensure_data_root

    ensure_data_root()
    configurations = (
        (WINDOWS_SERVICE_NAME, WINDOWS_SERVICE_DISPLAY_NAME, False, "NT AUTHORITY\\LocalService"),
        (
            WINDOWS_PROVISIONER_SERVICE_NAME,
            WINDOWS_PROVISIONER_SERVICE_DISPLAY_NAME,
            True,
            "LocalSystem",
        ),
    )
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        for name, display_name, provisioner, account in configurations:
            binary_path = service_command(exe_path, provisioner=provisioner)
            try:
                managed_service = win32service.OpenService(scm, name, win32service.SERVICE_ALL_ACCESS)
            except Exception as error:
                if _win32_error_code(error) != 1060:
                    raise ServiceError(f"could not open the existing {name} service: {error}") from error
                managed_service = win32service.CreateService(
                    scm,
                    name,
                    display_name,
                    win32service.SERVICE_ALL_ACCESS,
                    win32service.SERVICE_WIN32_OWN_PROCESS,
                    win32service.SERVICE_AUTO_START,
                    win32service.SERVICE_ERROR_NORMAL,
                    binary_path,
                    None,
                    0,
                    None,
                    account,
                    None,
                )
            else:
                configured = str(win32service.QueryServiceConfig(managed_service)[3]).strip().lower()
                if configured != binary_path.lower():
                    raise ServiceError(f"an existing {name} service is not managed by this installation")
                win32service.ChangeServiceConfig(
                    managed_service,
                    win32service.SERVICE_WIN32_OWN_PROCESS,
                    win32service.SERVICE_AUTO_START,
                    win32service.SERVICE_ERROR_NORMAL,
                    binary_path,
                    None,
                    0,
                    None,
                    account,
                    None,
                    display_name,
                )
            finally:
                win32service.CloseServiceHandle(managed_service)
            _configure_recovery(scm, name)
    finally:
        win32service.CloseServiceHandle(scm)


def _configure_recovery(scm, name: str = WINDOWS_SERVICE_NAME) -> None:  # pragma: no cover - Windows integration path
    _, _, _, win32service, _ = _win32_modules()
    service = win32service.OpenService(scm, name, win32service.SERVICE_ALL_ACCESS)
    try:
        actions = [
            (win32service.SC_ACTION_RESTART, 5000),
            (win32service.SC_ACTION_RESTART, 15000),
            (win32service.SC_ACTION_RESTART, 60000),
        ]
        win32service.ChangeServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
            {
                "ResetPeriod": 86400,
                "RebootMsg": "",
                "Command": "",
                "Actions": actions,
            },
        )
        win32service.ChangeServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG,
            True,
        )
    finally:
        win32service.CloseServiceHandle(service)


def start_service(name: str = WINDOWS_SERVICE_NAME) -> None:
    _, _, _, win32service, _ = _win32_modules()
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        service = win32service.OpenService(scm, name, win32service.SERVICE_START | win32service.SERVICE_QUERY_STATUS)
        try:
            if service_state(name) in {"stopped", "paused"}:
                win32service.StartService(service, [])
        finally:
            win32service.CloseServiceHandle(service)
    finally:
        win32service.CloseServiceHandle(scm)


def restart_service() -> None:
    for name in (WINDOWS_SERVICE_NAME, WINDOWS_PROVISIONER_SERVICE_NAME):
        if service_state(name) == "running":
            stop_service(name)
        start_service(name)


def stop_service(name: str = WINDOWS_SERVICE_NAME) -> None:
    if not service_installed(name):
        return
    _, _, _, win32service, _ = _win32_modules()
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        service = win32service.OpenService(scm, name, win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS)
        try:
            if service_state(name) == "running":
                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and service_state(name) not in {"stopped", "not installed"}:
                    time.sleep(0.1)
        finally:
            win32service.CloseServiceHandle(service)
    finally:
        win32service.CloseServiceHandle(scm)


def remove_service() -> None:
    if not is_admin():
        raise ServiceError("administrator rights are required to remove the Windows service")
    import win32con

    _, _, _, win32service, _ = _win32_modules()
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        for name, provisioner in (
            (WINDOWS_PROVISIONER_SERVICE_NAME, True),
            (WINDOWS_SERVICE_NAME, False),
        ):
            if not service_installed(name):
                continue
            if not service_managed(name=name, provisioner=provisioner):
                raise ServiceError(f"an existing {name} service is not managed by this installation")
            stop_service(name)
            managed_service = win32service.OpenService(scm, name, win32con.DELETE)
            try:
                win32service.DeleteService(managed_service)
            finally:
                win32service.CloseServiceHandle(managed_service)
    finally:
        win32service.CloseServiceHandle(scm)


class BridgeRuntime:
    """The service-owned credential, IPC, and IPP runtime."""

    def __init__(self):
        self.logger = get_logger()
        self.vault = CredentialVault()
        self.ipp = IppServer(self.submit_document, self.logger)
        self.control = ControlServer(self.handle_request, logger=self.logger)
        self._stop_lock = threading.Lock()
        self._stopped = False

    def start(self) -> None:
        self.logger.info("service startup")
        self.control.start()
        self.ipp.start()
        self.logger.info("listener started on 127.0.0.1:8631")

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self.logger.info("service shutdown")
        self.control.stop()
        self.ipp.shutdown()

    @staticmethod
    def _first(metadata: dict[str, Any], name: str, default: Any = None) -> Any:
        value = metadata.get(name, [default])
        return value[0] if isinstance(value, list) and value else value

    def submit_document(self, document: bytes, metadata: dict[str, Any], route_token: str) -> tuple[bool, str]:
        try:
            _, credentials = self.vault.profile_for_token(route_token)
        except CredentialError as error:
            return False, str(error)
        spool_dir().mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(prefix="job-", suffix=".pdf", dir=str(spool_dir()))
            with os.fdopen(fd, "wb") as stream:
                stream.write(document)
            sides = self._first(metadata, "sides", "one-sided")
            duplex = {
                "two-sided-long-edge": "LONG_EDGE",
                "two-sided-short-edge": "SHORT_EDGE",
            }.get(sides, "NO_DUPLEX")
            color_mode = self._first(metadata, "print-color-mode", "color")
            color = "STANDARD_MONOCHROME" if color_mode in {"monochrome", "bi-level"} else "STANDARD_COLOR"
            result = submit_pdf(
                Path(temporary_name),
                credentials,
                copies=int(self._first(metadata, "copies", 1)),
                duplex=duplex,
                color=color,
                title=str(self._first(metadata, "job-name", Path(temporary_name).stem)),
            )
            return result.accepted, result.message
        except (OSError, ValueError) as error:
            return False, f"could not prepare print job: {error}"
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except FileNotFoundError:
                    pass

    def handle_request(self, request: dict[str, Any], caller_sid: str) -> dict[str, Any]:
        operation = request.get("op")
        if operation == "ping":
            return {"ok": True}
        if operation == "status":
            try:
                _, route_token, username = self.vault.profile_for_user(caller_sid)
                credentials = "usable"
            except CredentialError:
                route_token = ""
                username = ""
                credentials = "missing or unusable"
            return {
                "ok": True,
                "credentials": credentials,
                "vault": self.vault.exists(caller_sid),
                "listener": self.ipp.ready,
                "last_job": self.ipp.last_job_status(),
                "route_token": route_token,
                "username": username,
            }
        if operation == "store":
            values = (request.get("jwt"), request.get("client_id"), request.get("org_id"))
            if not all(isinstance(value, str) and value for value in values):
                return {"ok": False, "error": "credential data is incomplete"}
            credentials = Credentials(
                values[0],
                values[1],
                values[2],
            )
            username = str(request.get("username", ""))
            route_token = self.vault.save_for_user(caller_sid, credentials, username)
            self.logger.info("enrollment result: credentials stored for user SID")
            return {"ok": True, "route_token": route_token}
        if operation == "clear":
            self.vault.clear_user(caller_sid)
            return {"ok": True}
        if operation == "test":
            encoded = request.get("document_b64")
            if not isinstance(encoded, str):
                return {"ok": False, "error": "test document is missing"}
            try:
                document = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                return {"ok": False, "error": "test document is invalid"}
            try:
                _, route_token, _ = self.vault.profile_for_user(caller_sid)
            except CredentialError as error:
                return {"ok": False, "error": str(error)}
            accepted, message = self.submit_document(
                document, {"job-name": [request.get("title", "test")]}, route_token
            )
            return {"ok": accepted, "accepted": accepted, "message": message}
        return {"ok": False, "error": "unknown control operation"}


class ProvisionerRuntime:
    """Privileged, credential-blind broker for per-user printer queues."""

    def __init__(self):
        self.logger = get_logger("hive_ipp_bridge.provisioner", "provisioner.log")
        self.control = ControlServer(
            self.handle_request,
            pipe_name=WINDOWS_PROVISIONER_PIPE_NAME,
            logger=self.logger,
        )

    def start(self) -> None:
        self.control.start()

    def stop(self) -> None:
        self.control.stop()

    def handle_request(self, request: dict[str, Any], caller_sid: str) -> dict[str, Any]:
        operation = request.get("op")
        if operation == "ping":
            return {"ok": True}
        supplied_route = request.get("route_token")
        if not isinstance(supplied_route, str) or not supplied_route:
            return {"ok": False, "error": "printer profile is incomplete"}
        route_token, username = load_user_route(caller_sid)
        if not secrets.compare_digest(supplied_route, route_token):
            return {"ok": False, "error": "printer route does not belong to the requesting user"}
        if operation == "ensure-printer":
            info = printer.ensure_printer(caller_sid, username, route_token)
            return {"ok": True, "printer_name": info.name}
        if operation == "remove-printer":
            removed = printer.remove_printer(caller_sid, username, route_token)
            return {"ok": True, "removed": removed}
        return {"ok": False, "error": "unknown provisioner operation"}


def run_service_process() -> None:  # pragma: no cover - Windows SCM integration path
    _, servicemanager, win32event, win32service, win32serviceutil = _win32_modules()

    class BridgeService(win32serviceutil.ServiceFramework):
        _svc_name_ = WINDOWS_SERVICE_NAME
        _svc_display_name_ = WINDOWS_SERVICE_DISPLAY_NAME
        _svc_description_ = "Local IPP bridge for the independent Hive IPP Bridge client."

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.runtime: BridgeRuntime | None = None

        def SvcStop(self):  # noqa: N802 - pywin32 API
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            if self.runtime:
                self.runtime.stop()

        def SvcDoRun(self):  # noqa: N802 - pywin32 API
            servicemanager.LogInfoMsg(f"{WINDOWS_SERVICE_NAME} starting")
            self.runtime = BridgeRuntime()
            try:
                self.runtime.start()
                win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            except Exception as error:
                servicemanager.LogErrorMsg(f"{WINDOWS_SERVICE_NAME} failed: {error}")
                raise
            finally:
                if self.runtime:
                    self.runtime.stop()

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(BridgeService)
    servicemanager.StartServiceCtrlDispatcher()


def run_provisioner_service_process() -> None:  # pragma: no cover - Windows SCM integration path
    _, servicemanager, win32event, win32service, win32serviceutil = _win32_modules()

    class ProvisionerService(win32serviceutil.ServiceFramework):
        _svc_name_ = WINDOWS_PROVISIONER_SERVICE_NAME
        _svc_display_name_ = WINDOWS_PROVISIONER_SERVICE_DISPLAY_NAME
        _svc_description_ = "Restricted printer provisioning broker for Hive IPP Bridge users."

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.runtime: ProvisionerRuntime | None = None

        def SvcStop(self):  # noqa: N802 - pywin32 API
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            if self.runtime:
                self.runtime.stop()

        def SvcDoRun(self):  # noqa: N802 - pywin32 API
            servicemanager.LogInfoMsg(f"{WINDOWS_PROVISIONER_SERVICE_NAME} starting")
            self.runtime = ProvisionerRuntime()
            try:
                self.runtime.start()
                win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            except Exception as error:
                servicemanager.LogErrorMsg(f"{WINDOWS_PROVISIONER_SERVICE_NAME} failed: {error}")
                raise
            finally:
                if self.runtime:
                    self.runtime.stop()

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(ProvisionerService)
    servicemanager.StartServiceCtrlDispatcher()
