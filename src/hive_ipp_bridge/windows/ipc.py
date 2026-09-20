"""Restricted named-pipe control protocol used by the Windows CLI and service."""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

from ..config import WINDOWS_PIPE_NAME


MAX_MESSAGE_BYTES = 128 * 1024 * 1024
PIPE_BUFFER_BYTES = 1024 * 1024


class IpcError(RuntimeError):
    """A safe local IPC failure."""


def encode_request(request: dict[str, Any]) -> bytes:
    payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise IpcError("control request is too large")
    return struct.pack("<I", len(payload)) + payload


def _decode_message(payload: bytes, *, message_type: str) -> dict[str, Any]:
    if len(payload) < 4:
        raise IpcError(f"control {message_type} is truncated")
    size = struct.unpack("<I", payload[:4])[0]
    body = payload[4:]
    if size != len(body) or size > MAX_MESSAGE_BYTES:
        raise IpcError(f"control {message_type} length is invalid")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IpcError(f"control {message_type} is not valid JSON") from error
    if not isinstance(value, dict):
        raise IpcError(f"control {message_type} is not an object")
    return value


def decode_request(payload: bytes) -> dict[str, Any]:
    value = _decode_message(payload, message_type="request")
    if not isinstance(value.get("op"), str):
        raise IpcError("control request has no operation")
    return value


def encode_response(response: dict[str, Any]) -> bytes:
    payload = json.dumps(response, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise IpcError("control response is too large")
    return struct.pack("<I", len(payload)) + payload


def decode_response(payload: bytes) -> dict[str, Any]:
    value = _decode_message(payload, message_type="response")
    if not isinstance(value.get("ok"), bool):
        raise IpcError("control response has no result")
    return value


def _read_exact(handle, win32file, size: int) -> bytes:  # pragma: no cover - Windows integration path
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        _, chunk = win32file.ReadFile(handle, min(PIPE_BUFFER_BYTES, remaining))
        if not chunk:
            raise IpcError("control pipe closed before the complete message arrived")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(handle, win32file, payload: bytes) -> None:  # pragma: no cover - Windows integration path
    offset = 0
    while offset < len(payload):
        chunk = payload[offset:offset + PIPE_BUFFER_BYTES]
        _, written = win32file.WriteFile(handle, chunk)
        offset += written or len(chunk)


def _write_response(handle, win32file, response: dict[str, Any]) -> None:
    """Write a complete response before allowing the server to disconnect."""

    _write_all(handle, win32file, encode_response(response))
    # DisconnectNamedPipe discards unread data. For a synchronous pipe server,
    # FlushFileBuffers waits until the client has consumed the response.
    win32file.FlushFileBuffers(handle)


def _windows_error_text(error: Exception) -> str:
    code = getattr(error, "winerror", None)
    if code is None and error.args and isinstance(error.args[0], int):
        code = error.args[0]
    return f"Windows error {code}" if code is not None else type(error).__name__


def _security_attributes():
    if os.name != "nt":
        return None
    try:
        import ntsecuritycon
        import pywintypes
        import win32security
    except ImportError as error:  # pragma: no cover - broken Windows environment
        raise IpcError("pywin32 is required for named-pipe security") from error

    dacl = pywintypes.ACL(1024)
    for sid_text in ("S-1-5-2", "S-1-5-7"):  # Network, Anonymous Logon
        sid = win32security.ConvertStringSidToSid(sid_text)
        dacl.AddAccessDeniedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    for sid_text in (
        "S-1-5-19",  # LocalService
        "S-1-5-18",  # SYSTEM
        "S-1-5-32-544",  # Builtin Administrators
    ):
        sid = win32security.ConvertStringSidToSid(sid_text)
        # The service-side creator needs FILE_CREATE_PIPE_INSTANCE, which is
        # included by FILE_ALL_ACCESS. The enrolled user below only needs to
        # connect and exchange control messages.
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    authenticated_users = win32security.ConvertStringSidToSid("S-1-5-11")
    # Avoid FILE_GENERIC_WRITE because it includes FILE_CREATE_PIPE_INSTANCE.
    client_access = ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_WRITE_DATA
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, client_access, authenticated_users)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SetSecurityDescriptorDacl(1, dacl, 0)
    return attributes


class ControlServer:
    """One-request-at-a-time named-pipe server for the service process."""

    def __init__(
        self,
        handler: Callable[[dict[str, Any], str], dict[str, Any]],
        pipe_name: str = WINDOWS_PIPE_NAME,
        logger: logging.Logger | None = None,
    ):
        self.handler = handler
        self.pipe_name = pipe_name
        self.logger = logger
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.name != "nt":
            raise IpcError("Windows named pipes are available only on Windows")
        self._thread = threading.Thread(target=self._serve, name="hive-ipp-control", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise IpcError("control pipe server timed out during startup")
        if self._startup_error is not None:
            raise IpcError(f"control pipe server could not start: {self._startup_error}")

    def stop(self) -> None:
        self._stop.set()
        # A short-lived client connection wakes ConnectNamedPipe on Windows.
        try:
            ControlClient(self.pipe_name).request({"op": "ping"}, timeout_ms=250)
        except IpcError:
            pass
        if self._thread:
            self._thread.join(timeout=2)

    def _serve(self) -> None:  # pragma: no cover - Windows integration path
        try:
            self._serve_loop()
        except Exception as error:
            self._startup_error = error
            self._ready.set()
            if self.logger:
                self.logger.exception("control pipe server failed")

    def _serve_loop(self) -> None:  # pragma: no cover - Windows integration path
        import pywintypes
        import win32file
        import win32pipe

        import win32api
        import win32security

        security = _security_attributes()
        while not self._stop.is_set():
            handle = win32pipe.CreateNamedPipe(
                self.pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                PIPE_BUFFER_BYTES,
                PIPE_BUFFER_BYTES,
                0,
                security,
            )
            self._ready.set()
            connected = False
            try:
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as error:
                    if getattr(error, "winerror", None) != 535:  # ERROR_PIPE_CONNECTED
                        raise
                connected = True
                if self._stop.is_set():
                    continue
                response: dict[str, Any]
                try:
                    prefix = _read_exact(handle, win32file, 4)
                    size = struct.unpack("<I", prefix)[0]
                    if size > MAX_MESSAGE_BYTES:
                        raise IpcError("control request is too large")
                    payload = prefix + _read_exact(handle, win32file, size)
                    request = decode_request(payload)
                    # pywin32 exposes this security operation from win32security,
                    # not win32pipe.  Keeping impersonation here ensures the
                    # handler receives the actual interactive user's SID.
                    win32security.ImpersonateNamedPipeClient(handle)
                    try:
                        token = win32security.OpenThreadToken(
                            win32api.GetCurrentThread(), win32security.TOKEN_QUERY, True
                        )
                        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
                        caller_sid = win32security.ConvertSidToStringSid(sid)
                    finally:
                        win32security.RevertToSelf()
                    try:
                        response = self.handler(request, caller_sid)
                    except Exception as error:  # return a safe failure without stopping IPC
                        response = {"ok": False, "error": str(error)}
                except Exception as error:
                    if self.logger:
                        self.logger.exception("control pipe request failed")
                    response = {
                        "ok": False,
                        "error": f"control request processing failed ({_windows_error_text(error)})",
                    }
                try:
                    _write_response(handle, win32file, response)
                except Exception:
                    if self.logger:
                        self.logger.exception("control pipe response failed")
            finally:
                if connected:
                    try:
                        win32pipe.DisconnectNamedPipe(handle)
                    except pywintypes.error:
                        pass
                try:
                    win32file.CloseHandle(handle)
                except pywintypes.error:
                    pass


class ControlClient:
    """Named-pipe client used by unprivileged CLI commands."""

    def __init__(self, pipe_name: str = WINDOWS_PIPE_NAME):
        self.pipe_name = pipe_name

    def request(self, request: dict[str, Any], timeout_ms: int = 5000) -> dict[str, Any]:
        if os.name != "nt":
            raise IpcError("Windows named pipes are available only on Windows")
        import ntsecuritycon
        import pywintypes
        import win32file

        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        handle = None
        while handle is None:
            try:
                handle = win32file.CreateFile(
                    self.pipe_name,
                    win32file.GENERIC_READ | ntsecuritycon.FILE_WRITE_DATA,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
            except pywintypes.error as error:
                if time.monotonic() >= deadline:
                    raise IpcError("Hive IPP Bridge service control pipe is unavailable") from error
                time.sleep(0.1)
        try:
            _write_all(handle, win32file, encode_request(request))
            prefix = _read_exact(handle, win32file, 4)
            size = struct.unpack("<I", prefix)[0]
            if size > MAX_MESSAGE_BYTES:
                raise IpcError("control response is too large")
            payload = prefix + _read_exact(handle, win32file, size)
            return decode_response(payload)
        except pywintypes.error as error:
            raise IpcError(
                f"Hive IPP Bridge service control request failed ({_windows_error_text(error)})"
            ) from error
        except IpcError:
            raise
        finally:
            win32file.CloseHandle(handle)


def request_with_document(document: bytes, **fields: Any) -> dict[str, Any]:
    """Build a test request without placing document data in a command argument."""

    request = dict(fields)
    request["document_b64"] = base64.b64encode(document).decode("ascii")
    return request
