"""Shared, non-secret application configuration."""

from __future__ import annotations

APPLICATION = "hive-ipp-bridge"

# Keep the existing Linux listener port stable while sharing it with Windows.
IPP_HOST = "127.0.0.1"
IPP_PORT = 8631
IPP_PATH = "/ipp/print"
LINUX_DEVICE_URI = f"ipp://localhost:{IPP_PORT}{IPP_PATH}"
WINDOWS_IPP_URL = f"http://{IPP_HOST}:{IPP_PORT}{IPP_PATH}"
IPP_PRINTER_URI = f"ipp://{IPP_HOST}:{IPP_PORT}{IPP_PATH}"
IPP_PRINTER_UUID = "8df4bf96-6c39-4b2f-a2a6-863186318631"


def windows_user_ipp_url(route_token: str) -> str:
    return f"http://{IPP_HOST}:{IPP_PORT}/ipp/{route_token}/print"


def windows_user_printer_uri(route_token: str) -> str:
    return f"ipp://{IPP_HOST}:{IPP_PORT}/ipp/{route_token}/print"

LINUX_QUEUE_NAME = "Hive_IPP_Bridge"
WINDOWS_PRINTER_NAME = "Hive IPP Bridge"

WINDOWS_SERVICE_NAME = "HiveIPPBridge"
WINDOWS_SERVICE_DISPLAY_NAME = "Hive IPP Bridge"
WINDOWS_PIPE_NAME = r"\\.\pipe\HiveIPPBridgeControl"
WINDOWS_PROVISIONER_SERVICE_NAME = "HiveIPPBridgeProvisioner"
WINDOWS_PROVISIONER_SERVICE_DISPLAY_NAME = "Hive IPP Bridge Provisioner"
WINDOWS_PROVISIONER_PIPE_NAME = r"\\.\pipe\HiveIPPBridgeProvisioner"

PROGRAM_DATA_DIR_NAME = "Hive IPP Bridge"
DEFAULT_ENDPOINT = "https://cloudnode.pmitc.papercut.com/print"

CREDENTIAL_KEYS = ("jwt", "client-id", "org-id")
