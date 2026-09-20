"""Service-owned DPAPI credential storage for Windows."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import secrets
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from ..enrollment import Credentials
from .paths import owner_sid_path, program_data_root, route_registry_path, vault_path


class CredentialError(RuntimeError):
    """A safe credential-storage failure."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _win32_error(message: str) -> CredentialError:
    error = ctypes.get_last_error()
    return CredentialError(f"{message} (Windows error {error})")


def _crypt(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise CredentialError("Windows DPAPI is available only on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    operation.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    operation.restype = wintypes.BOOL

    source = ctypes.create_string_buffer(data)
    input_blob = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    output_blob = _DataBlob()
    if not operation(ctypes.byref(input_blob), "Hive IPP Bridge", None, None, None, 0, ctypes.byref(output_blob)):
        raise _win32_error("DPAPI operation failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def protect(data: bytes) -> bytes:
    """Protect data for the current service account using user-scoped DPAPI."""

    return _crypt(data, protect=True)


def unprotect(data: bytes) -> bytes:
    return _crypt(data, protect=False)


def _ensure_windows_acl(path: Path, *, owner_sid: object | None = None) -> None:
    """Restrict data files to the service account, SYSTEM, and administrators."""

    if os.name != "nt":
        return
    try:
        import ntsecuritycon
        import win32security
    except ImportError as error:  # pragma: no cover - exercised only on a broken Windows install
        raise CredentialError("pywin32 is required for Windows credential ACLs") from error

    dacl = win32security.ACL(1024)
    principal_sids = (
        "S-1-5-19",  # LocalService
        "S-1-5-18",  # SYSTEM
        "S-1-5-32-544",  # Builtin Administrators
    )
    inheritance = 0
    if path.is_dir():
        inheritance = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
    for sid_text in principal_sids:
        sid = win32security.ConvertStringSidToSid(sid_text)
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            inheritance,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    if owner_sid is not None:
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            inheritance,
            ntsecuritycon.FILE_GENERIC_READ,
            owner_sid,
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, descriptor)


def ensure_data_root() -> Path:
    root = program_data_root()
    root.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _ensure_windows_acl(root)
    return root


def current_user_sid() -> object | None:
    if os.name != "nt":
        return None
    try:
        import win32api
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    except (ImportError, OSError):
        return None


def sid_text(sid: object) -> str:
    if os.name != "nt":
        return str(sid)
    import win32security

    return win32security.ConvertSidToStringSid(sid)


class CredentialVault:
    """Encrypted credential blob owned and accessed by the Windows service."""

    MAGIC = b"HIVEIPP-DPAPI-1\0"
    MULTIUSER_MAGIC = b"HIVEIPP-DPAPI-2\0"
    _lock = threading.RLock()

    def __init__(self, path: Path | None = None):
        self.path = path or vault_path()

    def exists(self, user_sid: str | None = None) -> bool:
        try:
            if not self.path.is_file():
                return False
            if user_sid is None:
                return True
            self.profile_for_user(user_sid)
            return True
        except OSError:
            return False
        except CredentialError:
            return False

    @staticmethod
    def validate(credentials: Credentials) -> None:
        if not all(isinstance(getattr(credentials, key_name), str) and getattr(credentials, key_name) for key_name in ("jwt", "client_id", "organization_id")):
            raise CredentialError("credential data is incomplete")

    def save(self, credentials: Credentials) -> None:
        self.validate(credentials)
        ensure_data_root()
        payload = json.dumps(asdict(credentials), separators=(",", ":")).encode("utf-8")
        encrypted = self.MAGIC + base64.b64encode(protect(payload))
        fd, temporary_name = tempfile.mkstemp(prefix="credentials.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
            _ensure_windows_acl(self.path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _read_records(self) -> tuple[dict[str, dict[str, object]], bool]:
        try:
            raw = self.path.read_bytes()
            if raw.startswith(self.MULTIUSER_MAGIC):
                encoded = raw[len(self.MULTIUSER_MAGIC):]
                values = json.loads(unprotect(base64.b64decode(encoded, validate=True)).decode("utf-8"))
                users = values.get("users")
                if not isinstance(users, dict):
                    raise CredentialError("credential vault format is invalid")
                return users, False
            if raw.startswith(self.MAGIC):
                encoded = raw[len(self.MAGIC):]
                values = json.loads(unprotect(base64.b64decode(encoded, validate=True)).decode("utf-8"))
                credentials = Credentials(values["jwt"], values["client_id"], values["organization_id"])
                self.validate(credentials)
                owner_sid = load_owner_sid_text()
                if not owner_sid:
                    raise CredentialError("legacy credential vault has no enrolled user")
                return {
                    owner_sid: {
                        "jwt": credentials.jwt,
                        "client_id": credentials.client_id,
                        "organization_id": credentials.organization_id,
                        "route_token": secrets.token_urlsafe(32),
                        "username": "",
                    }
                }, True
            raise CredentialError("credential vault format is invalid")
        except CredentialError:
            raise
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CredentialError("credential vault could not be read") from error

    def _write_records(self, users: dict[str, dict[str, object]]) -> None:
        ensure_data_root()
        payload = json.dumps({"users": users}, separators=(",", ":")).encode("utf-8")
        encrypted = self.MULTIUSER_MAGIC + base64.b64encode(protect(payload))
        fd, temporary_name = tempfile.mkstemp(prefix="credentials.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
            _ensure_windows_acl(self.path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        routes = {
            user_sid: {
                "route_token": str(record.get("route_token", "")),
                "username": str(record.get("username", "")),
            }
            for user_sid, record in users.items()
            if isinstance(record, dict) and record.get("route_token")
        }
        route_path = route_registry_path()
        route_fd, temporary_route = tempfile.mkstemp(prefix="routes.", dir=str(route_path.parent))
        try:
            with os.fdopen(route_fd, "w", encoding="utf-8") as stream:
                json.dump({"users": routes}, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_route, route_path)
            _ensure_windows_acl(route_path)
        finally:
            try:
                os.unlink(temporary_route)
            except FileNotFoundError:
                pass

    @staticmethod
    def _profile(record: dict[str, object]) -> tuple[Credentials, str, str]:
        try:
            credentials = Credentials(
                str(record["jwt"]), str(record["client_id"]), str(record["organization_id"])
            )
            route_token = str(record["route_token"])
            username = str(record.get("username", ""))
        except (KeyError, TypeError) as error:
            raise CredentialError("credential profile is invalid") from error
        CredentialVault.validate(credentials)
        if not route_token or "/" in route_token or "\\" in route_token:
            raise CredentialError("credential route token is invalid")
        return credentials, route_token, username

    def profile_for_user(self, user_sid: str) -> tuple[Credentials, str, str]:
        with self._lock:
            users, migrated = self._read_records()
            record = users.get(user_sid)
            if not isinstance(record, dict):
                raise CredentialError("credentials are not enrolled for this Windows user")
            profile = self._profile(record)
            route_token = profile[1]
            try:
                registered_route, _ = load_user_route(user_sid)
            except CredentialError:
                registered_route = ""
            if migrated or not secrets.compare_digest(registered_route, route_token):
                self._write_records(users)
            return profile

    def save_for_user(self, user_sid: str, credentials: Credentials, username: str = "") -> str:
        self.validate(credentials)
        if not user_sid.startswith("S-1-"):
            raise CredentialError("Windows user SID is invalid")
        with self._lock:
            try:
                users, _ = self._read_records()
            except CredentialError:
                if self.path.exists():
                    raise
                users = {}
            existing = users.get(user_sid)
            route_token = (
                str(existing.get("route_token"))
                if isinstance(existing, dict) and existing.get("route_token")
                else secrets.token_urlsafe(32)
            )
            users[user_sid] = {
                "jwt": credentials.jwt,
                "client_id": credentials.client_id,
                "organization_id": credentials.organization_id,
                "route_token": route_token,
                "username": username,
            }
            self._write_records(users)
            return route_token

    def profile_for_token(self, route_token: str) -> tuple[str, Credentials]:
        with self._lock:
            users, migrated = self._read_records()
            for user_sid, record in users.items():
                if isinstance(record, dict) and record.get("route_token") == route_token:
                    credentials, _, _ = self._profile(record)
                    if migrated:
                        self._write_records(users)
                    return user_sid, credentials
        raise CredentialError("print route is not enrolled")

    def clear_user(self, user_sid: str) -> None:
        with self._lock:
            try:
                users, _ = self._read_records()
            except CredentialError:
                return
            if users.pop(user_sid, None) is None:
                return
            if users:
                self._write_records(users)
            else:
                self._write_records({})
                self.clear()

    def load(self) -> Credentials:
        try:
            raw = self.path.read_bytes()
            if not raw.startswith(self.MAGIC):
                raise CredentialError("credential vault format is invalid")
            decoded = base64.b64decode(raw[len(self.MAGIC):], validate=True)
            values = json.loads(unprotect(decoded).decode("utf-8"))
            credentials = Credentials(values["jwt"], values["client_id"], values["organization_id"])
            self.validate(credentials)
            return credentials
        except CredentialError:
            raise
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CredentialError("credential vault could not be read") from error

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise CredentialError("credential vault could not be removed") from error
        if self.path == vault_path():
            try:
                route_registry_path().unlink()
            except FileNotFoundError:
                pass


def load_user_route(user_sid: str) -> tuple[str, str]:
    """Read the non-credential SID-to-route registry as a machine service."""

    try:
        values = json.loads(route_registry_path().read_text(encoding="utf-8"))
        record = values["users"][user_sid]
        route_token = str(record["route_token"])
        username = str(record.get("username", ""))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CredentialError("no provisionable print route exists for this Windows user") from error
    if not route_token:
        raise CredentialError("print route is invalid")
    return route_token, username


def save_owner_sid(sid: object | None) -> None:
    if sid is None:
        return
    ensure_data_root()
    path = owner_sid_path()
    path.write_text(sid_text(sid), encoding="ascii")
    _ensure_windows_acl(path)


def load_owner_sid_text() -> str | None:
    try:
        value = owner_sid_path().read_text(encoding="ascii").strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    return value or None


def clear_owner_sid() -> None:
    try:
        owner_sid_path().unlink()
    except FileNotFoundError:
        pass
