"""Windows installation and data paths."""

from __future__ import annotations

import os
from pathlib import Path

from ..config import PROGRAM_DATA_DIR_NAME


def program_data_root() -> Path:
    base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return Path(base) / PROGRAM_DATA_DIR_NAME


def log_dir() -> Path:
    return program_data_root() / "logs"


def spool_dir() -> Path:
    return program_data_root() / "spool"


def vault_path() -> Path:
    return program_data_root() / "credentials.dpapi"


def owner_sid_path() -> Path:
    return program_data_root() / "owner.sid"


def route_registry_path() -> Path:
    return program_data_root() / "routes.json"
