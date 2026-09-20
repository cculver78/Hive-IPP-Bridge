"""Windows IPP printer creation and safety checks."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass

from ..config import IPP_PRINTER_UUID, WINDOWS_IPP_URL, WINDOWS_PRINTER_NAME, windows_user_ipp_url


EXPECTED_DRIVER = "Microsoft IPP Class Driver"


class PrinterError(RuntimeError):
    """A safe printer-management failure."""


@dataclass(frozen=True)
class PrinterInfo:
    name: str
    driver_name: str
    port_name: str
    uri_values: tuple[str, ...]
    printer_uuid: str = ""

    @property
    def bridge_mismatch_reason(self) -> str:
        reasons: list[str] = []
        if self.driver_name != EXPECTED_DRIVER:
            reasons.append(f"driver is '{self.driver_name or '<missing>'}'")
        has_bridge_uri = any(
            normalize_uri(value).startswith("http://127.0.0.1:8631/ipp/")
            and normalize_uri(value).endswith("/print")
            for value in self.uri_values
            if value
        )
        has_bridge_wsd_identity = (
            self.port_name.upper().startswith("WSD-")
            and self.printer_uuid.lower() == IPP_PRINTER_UUID
        )
        if not has_bridge_uri and not has_bridge_wsd_identity:
            if self.port_name.upper().startswith("WSD-"):
                found_uuid = self.printer_uuid or "missing"
                reasons.append(
                    f"WSD printer UUID is '{found_uuid}', expected '{IPP_PRINTER_UUID}'"
                )
            else:
                reasons.append("no managed localhost IPP URI or WSD printer identity was found")
        return "; ".join(reasons)

    @property
    def matches_bridge(self) -> bool:
        return not self.bridge_mismatch_reason

    def matches_profile(self, name: str, ipp_url: str) -> bool:
        expected = normalize_uri(ipp_url)
        uri_matches = expected in {normalize_uri(value) for value in self.uri_values if value}
        return (
            self.name == name
            and self.driver_name == EXPECTED_DRIVER
            and uri_matches
        )


def user_printer_name(username: str, user_sid: str) -> str:
    display = username.rsplit("\\", 1)[-1].strip() or "User"
    display = re.sub(r"[^A-Za-z0-9._ -]", "_", display)[:32]
    suffix = hashlib.sha256(user_sid.encode("ascii")).hexdigest()[:8]
    return f"{WINDOWS_PRINTER_NAME} ({display}-{suffix})"


def normalize_uri(value: str) -> str:
    normalized = value.strip().rstrip("/").lower().replace("localhost", "127.0.0.1")
    if normalized.startswith("ipp://"):
        normalized = "http://" + normalized.removeprefix("ipp://")
    return normalized


def _powershell(script: str) -> str:
    if os.name != "nt":
        raise PrinterError("Windows printer management is available only on Windows")
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "Windows printer command failed"
        raise PrinterError(f"{message} (exit code {result.returncode})")
    return result.stdout.strip()


def _json_value(value: str) -> str:
    return json.dumps(value)


def inspect_printer(name_value: str = WINDOWS_PRINTER_NAME) -> PrinterInfo | None:
    name = _json_value(name_value)
    script = f"""
$p = Get-Printer -Name {name} -ErrorAction SilentlyContinue
if ($null -eq $p) {{ exit 3 }}
$port = Get-PrinterPort -Name $p.PortName -ErrorAction SilentlyContinue
$portMeta = Get-ItemProperty -LiteralPath (Join-Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Print\\Monitors\\WSD Port\\Ports' $p.PortName) -ErrorAction SilentlyContinue
[pscustomobject]@{{
  Name = [string]$p.Name
  DriverName = [string]$p.DriverName
  PortName = [string]$p.PortName
  DeviceUrl = [string]$p.DeviceUrl
  PrinterUri = [string]$p.PrinterUri
  PortAddress = [string]$port.PrinterHostAddress
  PortNameValue = [string]$port.Name
  PrinterUuid = [string]$portMeta.'Printer UUID'
}} | ConvertTo-Json -Compress
"""
    try:
        raw = _powershell(script)
    except PrinterError as error:
        if "exit code 3" in str(error).lower():
            return None
        raise
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PrinterError("Windows returned invalid printer metadata") from error
    uri_values = tuple(
        str(values.get(key, ""))
        for key in ("DeviceUrl", "PrinterUri", "PortAddress", "PortNameValue", "PortName")
        if values.get(key)
    )
    return PrinterInfo(
        str(values.get("Name", "")),
        str(values.get("DriverName", "")),
        str(values.get("PortName", "")),
        uri_values,
        str(values.get("PrinterUuid", "")),
    )


def ensure_printer(user_sid: str, username: str, route_token: str) -> PrinterInfo:
    name_value = user_printer_name(username, user_sid)
    ipp_url = windows_user_ipp_url(route_token)
    existing = inspect_printer(name_value)
    if existing:
        if not existing.matches_profile(name_value, ipp_url):
            # Migrate the queue created by the earlier -IppURL implementation.
            # That command creates a WSD port, but this bridge exposes a direct
            # localhost HTTP IPP endpoint rather than a WS-Discovery device.
            # Only remove it when its driver and WSD identity prove it belongs
            # to this bridge; unrelated queues remain protected.
            legacy_wsd = (
                existing.driver_name == EXPECTED_DRIVER
                and existing.port_name.upper().startswith("WSD-")
                and existing.printer_uuid.lower() == IPP_PRINTER_UUID
            )
            if not legacy_wsd:
                raise PrinterError(
                    f"{name_value} exists but does not use the expected direct IPP URL and driver; leaving it untouched"
                )
            _powershell(f"Remove-Printer -Name {_json_value(name_value)} -ErrorAction Stop")
        else:
            return existing

    # PRINTER_ACCESS_USE | READ_CONTROL for the enrolled user; full printer
    # control for SYSTEM and Administrators. No Everyone print ACE is present.
    permission_sddl = (
        f"D:(A;;0x00020008;;;{user_sid})"
        "(A;;0x000F000C;;;SY)(A;;0x000F000C;;;BA)"
    )
    script = f"""
$driver = Get-PrinterDriver -Name {_json_value(EXPECTED_DRIVER)} -ErrorAction SilentlyContinue
if ($null -eq $driver) {{ throw 'Microsoft IPP Class Driver is not available' }}
Add-Printer -Name {_json_value(name_value)} -DriverName {_json_value(EXPECTED_DRIVER)} -PortName {_json_value(ipp_url)} -PermissionSDDL {_json_value(permission_sddl)} -ErrorAction Stop
"""
    _powershell(script)
    created = inspect_printer(name_value)
    if not created or not created.matches_profile(name_value, ipp_url):
        raise PrinterError("Windows created the printer, but its URI or driver could not be verified")
    return created


def remove_printer(user_sid: str, username: str, route_token: str) -> bool:
    name_value = user_printer_name(username, user_sid)
    ipp_url = windows_user_ipp_url(route_token)
    existing = inspect_printer(name_value)
    if not existing:
        return False
    if not existing.matches_profile(name_value, ipp_url):
        raise PrinterError(
            f"{name_value} does not match the managed bridge URI and driver; leaving it untouched"
        )
    script = f"Remove-Printer -Name {_json_value(name_value)} -ErrorAction Stop"
    _powershell(script)
    return True


def managed_printers() -> list[PrinterInfo]:
    prefix = _json_value(f"{WINDOWS_PRINTER_NAME} (")
    script = f"""
$items = @(Get-Printer | Where-Object {{ $_.Name.StartsWith({prefix}) }}) | ForEach-Object {{
  $port = Get-PrinterPort -Name $_.PortName -ErrorAction SilentlyContinue
  $portMeta = Get-ItemProperty -LiteralPath (Join-Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Print\\Monitors\\WSD Port\\Ports' $_.PortName) -ErrorAction SilentlyContinue
  [pscustomobject]@{{
    Name = [string]$_.Name
    DriverName = [string]$_.DriverName
    PortName = [string]$_.PortName
    DeviceUrl = [string]$_.DeviceUrl
    PrinterUri = [string]$_.PrinterUri
    PortAddress = [string]$port.PrinterHostAddress
    PortNameValue = [string]$port.Name
    PrinterUuid = [string]$portMeta.'Printer UUID'
  }}
}}
ConvertTo-Json -Compress -InputObject @($items)
"""
    raw = _powershell(script)
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PrinterError("Windows returned invalid printer metadata") from error
    if isinstance(values, dict):
        values = [values]
    result = []
    for value in values:
        uri_values = tuple(
            str(value.get(key, ""))
            for key in ("DeviceUrl", "PrinterUri", "PortAddress", "PortNameValue", "PortName")
            if value.get(key)
        )
        result.append(
            PrinterInfo(
                str(value.get("Name", "")),
                str(value.get("DriverName", "")),
                str(value.get("PortName", "")),
                uri_values,
                str(value.get("PrinterUuid", "")),
            )
        )
    return result


def remove_all_managed_printers() -> int:
    printers = managed_printers()
    unsafe = [item for item in printers if not item.matches_bridge]
    if unsafe:
        raise PrinterError(
            "refusing to remove printer(s) that do not match the managed Hive identity: "
            + ", ".join(f"{item.name} ({item.bridge_mismatch_reason})" for item in unsafe)
        )
    for item in printers:
        _powershell(f"Remove-Printer -Name {_json_value(item.name)} -ErrorAction Stop")
    return len(printers)


def remove_legacy_single_user_printer() -> bool:
    existing = inspect_printer(WINDOWS_PRINTER_NAME)
    if not existing:
        return False
    if not existing.matches_profile(WINDOWS_PRINTER_NAME, WINDOWS_IPP_URL):
        raise PrinterError(
            f"{WINDOWS_PRINTER_NAME} exists but is not the legacy managed queue; leaving it untouched"
        )
    _powershell(f"Remove-Printer -Name {_json_value(WINDOWS_PRINTER_NAME)} -ErrorAction Stop")
    return True
