#!/usr/bin/env python3
"""Submit a PDF print job to a PaperCut Hive cloud node.

Developed by Edge Case Software — https://edgecasesoftware.dev
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import secrets
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from .config import DEFAULT_ENDPOINT
    from .enrollment import Credentials
except ImportError:  # Support the existing Linux ippeveprinter adapter's script-mode invocation.
    from config import DEFAULT_ENDPOINT
    from enrollment import Credentials


CLIENT_TYPE = "ChromeApp-2.4.1"


@dataclass(frozen=True)
class SubmissionResult:
    """Safe result information returned by a Hive submission."""

    accepted: bool
    message: str
    status_code: int | None = None


def request_id() -> str:
    prefix = "".join(
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
        for _ in range(16)
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{timestamp}"


def multipart_body(fields: dict[str, str], pdf: Path) -> tuple[bytes, str]:
    boundary = f"----PaperCutHive{secrets.token_hex(16)}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )

    mime_type = mimetypes.guess_type(pdf.name)[0] or "application/pdf"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="printDocument"; '
                f'filename="{pdf.name}"\r\n'
            ).encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(),
            pdf.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), boundary


def _validate_document(pdf: Path, copies: int) -> str | None:
    if not pdf.is_file():
        return f"PDF not found: {pdf}"
    if pdf.suffix.lower() != ".pdf":
        return "PaperCut Hive expects an application/pdf document"
    if copies < 1 or copies > 100:
        return "copies must be between 1 and 100"
    return None


def submit_pdf(
    pdf: Path,
    credentials: Credentials,
    *,
    copies: int = 1,
    duplex: str = "NO_DUPLEX",
    color: str = "STANDARD_COLOR",
    width: int = 215900,
    height: int = 279400,
    title: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
) -> SubmissionResult:
    """Submit a PDF with explicit credentials without exposing them to a shell."""

    validation_error = _validate_document(pdf, copies)
    if validation_error:
        return SubmissionResult(False, validation_error)
    if duplex not in {"NO_DUPLEX", "LONG_EDGE", "SHORT_EDGE"}:
        return SubmissionResult(False, "invalid duplex setting")
    if color not in {"STANDARD_COLOR", "STANDARD_MONOCHROME", "AUTO"}:
        return SubmissionResult(False, "invalid color setting")

    fields = {
        "copies": str(copies),
        "duplex": duplex,
        "color": color,
        "mediaWidthMicrons": str(width),
        "mediaHeightMicrons": str(height),
        "fileFormat": "application/pdf",
        "documentName": title or pdf.stem,
    }
    body, boundary = multipart_body(fields, pdf)
    headers = {
        "Authorization": f"Bearer {credentials.jwt}",
        "client-id": credentials.client_id,
        "client-type": CLIENT_TYPE,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Encrypted": "true",
        "PMITC-PrintRequest-Id": request_id(),
        "X-Correlation-ID": f"CHROME-PRINT-CLIENT|{secrets.token_urlsafe(15)[:20]}",
        "X-PMITC-OrgId": credentials.organization_id,
    }
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                return SubmissionResult(False, f"PaperCut returned HTTP {response.status}", response.status)
    except urllib.error.HTTPError as error:
        return SubmissionResult(False, f"PaperCut returned HTTP {error.code}", error.code)
    except urllib.error.URLError as error:
        return SubmissionResult(False, f"unable to reach PaperCut: {error.reason}")

    return SubmissionResult(True, "PaperCut Hive accepted the print job.", 200)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="PDF file to submit")
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument(
        "--duplex", choices=("NO_DUPLEX", "LONG_EDGE", "SHORT_EDGE"), default="NO_DUPLEX"
    )
    parser.add_argument(
        "--color", choices=("STANDARD_COLOR", "STANDARD_MONOCHROME", "AUTO"),
        default="STANDARD_COLOR",
    )
    parser.add_argument("--width", type=int, default=215900, help="Media width in microns")
    parser.add_argument("--height", type=int, default=279400, help="Media height in microns")
    parser.add_argument("--title", help="Print-job title; defaults to the filename")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--dry-run", action="store_true", help="Validate and describe without uploading")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validation_error = _validate_document(args.pdf, args.copies)
    if validation_error:
        print(f"error: {validation_error}", file=sys.stderr)
        return 2

    fields = {
        "copies": str(args.copies),
        "duplex": args.duplex,
        "color": args.color,
        "mediaWidthMicrons": str(args.width),
        "mediaHeightMicrons": str(args.height),
        "fileFormat": "application/pdf",
        "documentName": args.title or args.pdf.stem,
    }
    if args.dry_run:
        print(f"Ready to submit {args.pdf} to {args.endpoint}")
        for name, value in fields.items():
            print(f"{name}={value}")
        return 0

    credential_names = ("PAPERCUT_HIVE_JWT", "PAPERCUT_HIVE_CLIENT_ID", "PAPERCUT_HIVE_ORG_ID")
    values = {name: os.environ.get(name) for name in credential_names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        print(f"error: missing environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    result = submit_pdf(
        args.pdf,
        Credentials(values[credential_names[0]] or "", values[credential_names[1]] or "", values[credential_names[2]] or ""),
        copies=args.copies,
        duplex=args.duplex,
        color=args.color,
        width=args.width,
        height=args.height,
        title=args.title,
        endpoint=args.endpoint,
    )
    if not result.accepted:
        print(f"error: {result.message}", file=sys.stderr)
        return 1
    print(result.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
