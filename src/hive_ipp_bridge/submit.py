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
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ENDPOINT = "https://cloudnode.pmitc.papercut.com/print"
CLIENT_TYPE = "ChromeApp-2.4.1"


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
    if not args.pdf.is_file():
        print(f"error: PDF not found: {args.pdf}", file=sys.stderr)
        return 2
    if args.pdf.suffix.lower() != ".pdf":
        print("error: PaperCut Hive expects an application/pdf document", file=sys.stderr)
        return 2
    if args.copies < 1 or args.copies > 100:
        print("error: copies must be between 1 and 100", file=sys.stderr)
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
    credentials = {name: os.environ.get(name) for name in credential_names}
    missing = [name for name, value in credentials.items() if not value]
    if missing:
        print(f"error: missing environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    body, boundary = multipart_body(fields, args.pdf)
    headers = {
        "Authorization": f"Bearer {credentials['PAPERCUT_HIVE_JWT']}",
        "client-id": credentials["PAPERCUT_HIVE_CLIENT_ID"],
        "client-type": CLIENT_TYPE,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Encrypted": "true",
        "PMITC-PrintRequest-Id": request_id(),
        "X-Correlation-ID": f"CHROME-PRINT-CLIENT|{secrets.token_urlsafe(15)[:20]}",
        "X-PMITC-OrgId": credentials["PAPERCUT_HIVE_ORG_ID"],
    }
    request = urllib.request.Request(args.endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                print(f"error: PaperCut returned HTTP {response.status}", file=sys.stderr)
                return 1
    except urllib.error.HTTPError as error:
        print(f"error: PaperCut returned HTTP {error.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"error: unable to reach PaperCut: {error.reason}", file=sys.stderr)
        return 1

    print("PaperCut Hive accepted the print job.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
