#!/usr/bin/env python3
"""Translate an ippeveprinter job into a PaperCut Hive cloud submission.

Developed by Edge Case Software — https://edgecasesoftware.dev
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


MEDIA_MICRONS = {
    "iso_a4_210x297mm": (210000, 297000),
    "na_letter_8.5x11in": (215900, 279400),
    "na_legal_8.5x14in": (215900, 355600),
    "na_ledger_11x17in": (279400, 431800),
}


def secret(key: str) -> str:
    result = subprocess.run(
        ["secret-tool", "lookup", "application", "hive-ipp-bridge", "key", key],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"credential '{key}' is not available in the desktop keyring")
    return result.stdout.strip()


def main() -> int:
    if len(sys.argv) != 2:
        print("ERROR: PaperCut adapter expected one spool filename", file=sys.stderr)
        return 1

    document = Path(sys.argv[1])
    if not document.is_file():
        print(f"ERROR: PaperCut adapter cannot read {document}", file=sys.stderr)
        return 1

    media = os.environ.get("IPP_MEDIA", os.environ.get("IPP_MEDIA_DEFAULT", ""))
    width, height = MEDIA_MICRONS.get(media.lower(), (215900, 279400))
    sides = os.environ.get("IPP_SIDES", "one-sided")
    duplex = {
        "two-sided-long-edge": "LONG_EDGE",
        "two-sided-short-edge": "SHORT_EDGE",
    }.get(sides, "NO_DUPLEX")
    color_mode = os.environ.get("IPP_PRINT_COLOR_MODE", "color")
    color = "STANDARD_MONOCHROME" if color_mode in {"monochrome", "bi-level"} else "STANDARD_COLOR"

    try:
        credentials = {
            "PAPERCUT_HIVE_JWT": secret("jwt"),
            "PAPERCUT_HIVE_CLIENT_ID": secret("client-id"),
            "PAPERCUT_HIVE_ORG_ID": secret("org-id"),
        }
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    submitter = Path(__file__).with_name("submit.py")
    command = [
        sys.executable,
        str(submitter),
        str(document),
        "--copies",
        os.environ.get("IPP_COPIES", "1"),
        "--duplex",
        duplex,
        "--color",
        color,
        "--width",
        str(width),
        "--height",
        str(height),
        "--title",
        os.environ.get("IPP_JOB_NAME", document.stem),
    ]
    environment = os.environ.copy()
    environment.update(credentials)
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "PaperCut submission failed"
        print(f"ERROR: {message}", file=sys.stderr)
        return 1

    print("INFO: PaperCut Hive accepted the print job", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
