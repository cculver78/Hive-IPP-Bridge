"""Small localhost IPP server for the Windows Microsoft IPP Class Driver."""

from __future__ import annotations

import http.server
import logging
import socketserver
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import IPP_HOST, IPP_PORT, IPP_PRINTER_UUID, WINDOWS_PRINTER_NAME, windows_user_printer_uri


MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
ACCEPTED_DOCUMENT_FORMATS = {"application/pdf", "application/octet-stream"}

OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_CREATE_JOB = 0x0005
OP_SEND_DOCUMENT = 0x0006
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B

STATUS_OK = 0x0000
STATUS_CLIENT_BAD_REQUEST = 0x0400
STATUS_CLIENT_NOT_FOUND = 0x0406
STATUS_CLIENT_DOCUMENT_FORMAT = 0x040B
STATUS_SERVER_ERROR = 0x0500

TAG_OPERATION_ATTRIBUTES = 0x01
TAG_JOB_ATTRIBUTES = 0x02
TAG_END = 0x03
TAG_PRINTER_ATTRIBUTES = 0x04
TAG_UNSUPPORTED_ATTRIBUTES = 0x05

VALUE_INTEGER = 0x21
VALUE_BOOLEAN = 0x22
VALUE_ENUM = 0x23
VALUE_RANGE = 0x33
VALUE_TEXT = 0x41
VALUE_NAME = 0x42
VALUE_KEYWORD = 0x44
VALUE_URI = 0x45
VALUE_CHARSET = 0x47
VALUE_LANGUAGE = 0x48
VALUE_MIMETYPE = 0x49


class IppProtocolError(ValueError):
    """Malformed IPP request."""


@dataclass(frozen=True)
class IppRequest:
    version: tuple[int, int]
    operation: int
    request_id: int
    attributes: dict[str, list[Any]]
    data: bytes

    def first(self, name: str, default: Any = None) -> Any:
        values = self.attributes.get(name)
        return values[0] if values else default


def _decode_value(tag: int, value: bytes) -> Any:
    if tag in {VALUE_INTEGER, VALUE_ENUM}:
        if len(value) != 4:
            raise IppProtocolError("invalid integer attribute")
        return struct.unpack(">i", value)[0]
    if tag == VALUE_RANGE:
        if len(value) != 8:
            raise IppProtocolError("invalid range attribute")
        return struct.unpack(">ii", value)
    if tag == VALUE_BOOLEAN:
        if len(value) != 1:
            raise IppProtocolError("invalid boolean attribute")
        return value != b"\0"
    if tag in {VALUE_TEXT, VALUE_NAME, VALUE_KEYWORD, VALUE_URI, VALUE_CHARSET, VALUE_LANGUAGE, VALUE_MIMETYPE}:
        return value.decode("utf-8", errors="replace")
    return value


def parse_request(payload: bytes) -> IppRequest:
    if len(payload) < 8:
        raise IppProtocolError("IPP request header is truncated")
    major, minor, operation, request_id = struct.unpack(">BBHI", payload[:8])
    offset = 8
    current_group: int | None = None
    current_name = ""
    attributes: dict[str, list[Any]] = {}
    while offset < len(payload):
        tag = payload[offset]
        offset += 1
        if tag == TAG_END:
            return IppRequest((major, minor), operation, request_id, attributes, payload[offset:])
        if tag in {TAG_OPERATION_ATTRIBUTES, TAG_JOB_ATTRIBUTES, TAG_PRINTER_ATTRIBUTES, TAG_UNSUPPORTED_ATTRIBUTES}:
            current_group = tag
            current_name = ""
            continue
        if current_group is None or offset + 4 > len(payload):
            raise IppProtocolError("IPP attribute group is invalid")
        name_length = struct.unpack(">H", payload[offset:offset + 2])[0]
        offset += 2
        if offset + name_length + 2 > len(payload):
            raise IppProtocolError("IPP attribute name is truncated")
        if name_length:
            current_name = payload[offset:offset + name_length].decode("utf-8", errors="replace")
        offset += name_length
        value_length = struct.unpack(">H", payload[offset:offset + 2])[0]
        offset += 2
        if offset + value_length > len(payload):
            raise IppProtocolError("IPP attribute value is truncated")
        if not current_name:
            raise IppProtocolError("IPP repeated attribute has no prior name")
        value = _decode_value(tag, payload[offset:offset + value_length])
        offset += value_length
        attributes.setdefault(current_name, []).append(value)
    raise IppProtocolError("IPP request has no end-of-attributes tag")


def _encode_attribute(tag: int, name: str, value: Any, *, include_name: bool = True) -> bytes:
    if isinstance(value, bool):
        raw = b"\x01" if value else b"\0"
        tag = VALUE_BOOLEAN
    elif isinstance(value, tuple) and len(value) == 2:
        raw = struct.pack(">ii", int(value[0]), int(value[1]))
        tag = VALUE_RANGE
    elif isinstance(value, int):
        raw = struct.pack(">i", value)
        tag = VALUE_INTEGER if tag not in {VALUE_ENUM} else tag
    else:
        raw = str(value).encode("utf-8")
    name_bytes = name.encode("utf-8") if include_name else b""
    return bytes([tag]) + struct.pack(">H", len(name_bytes)) + name_bytes + struct.pack(">H", len(raw)) + raw


def _encode_group(group_tag: int, attributes: list[tuple[int, str, Any]]) -> bytes:
    previous_name = None
    encoded: list[bytes] = []
    for tag, name, value in attributes:
        encoded.append(_encode_attribute(tag, name, value, include_name=name != previous_name))
        previous_name = name
    return bytes([group_tag]) + b"".join(encoded)


def encode_response(
    request: IppRequest,
    status: int,
    groups: list[bytes] | None = None,
) -> bytes:
    operation = struct.pack(">BBHI", request.version[0], request.version[1], status, request.request_id)
    operation_group = _encode_group(
        TAG_OPERATION_ATTRIBUTES,
        [
            (VALUE_CHARSET, "attributes-charset", "utf-8"),
            (VALUE_LANGUAGE, "attributes-natural-language", "en"),
        ],
    )
    return operation + operation_group + b"".join(groups or []) + bytes([TAG_END])


@dataclass
class Job:
    job_id: int
    route_token: str = ""
    document: bytes = b""
    name: str = "Print Job"
    state: int = 3  # pending
    reason: str = "job-data-insufficient"
    metadata: dict[str, Any] = field(default_factory=dict)


class JobStore:
    def __init__(self, submitter: Callable[[bytes, dict[str, Any], str], tuple[bool, str]], logger: logging.Logger):
        self.submitter = submitter
        self.logger = logger
        self.jobs: dict[int, Job] = {}
        self.lock = threading.RLock()
        self.next_id = 1
        self.last_job_status: dict[str, object] | None = None

    def create(self, request: IppRequest, route_token: str = "") -> Job:
        with self.lock:
            job = Job(
                self.next_id,
                route_token=route_token,
                name=str(request.first("job-name", request.first("document-name", "Print Job"))),
                metadata=dict(request.attributes),
            )
            self.next_id += 1
            self.jobs[job.job_id] = job
            return job

    def get(self, job_id: int, route_token: str | None = None) -> Job | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None and route_token is not None and job.route_token != route_token:
                return None
            return job

    def accept(self, job: Job, document: bytes, *, last_document: bool = True) -> None:
        if len(document) > MAX_DOCUMENT_BYTES:
            raise IppProtocolError("print document is too large")
        # Windows may split one PDF across multiple Send-Document requests.
        # Only the first non-empty chunk has the PDF header.
        if not job.document and document and not document.startswith(b"%PDF-"):
            raise IppProtocolError("only application/pdf print documents are supported")
        with self.lock:
            job.document += document
            job.state = 5 if last_document else 3
            job.reason = "none" if last_document else "job-data-insufficient"
        if last_document:
            threading.Thread(target=self._submit, args=(job,), name=f"hive-job-{job.job_id}", daemon=True).start()

    def _submit(self, job: Job) -> None:
        self.logger.info("received print job %s", job.job_id)
        try:
            accepted, message = self.submitter(job.document, job.metadata, job.route_token)
        except Exception as error:  # the service must report a failed job, not terminate
            accepted, message = False, str(error)
        with self.lock:
            job.state = 9 if accepted else 7
            job.reason = "none" if accepted else str(message)[:240]
            self.last_job_status = {
                "id": job.job_id,
                "state": "completed" if accepted else "failed",
                "message": str(message)[:240],
                "bytes": len(job.document),
            }
        self.logger.info(
            "print job %s submission result: %s (%s)",
            job.job_id,
            "accepted" if accepted else "failed",
            str(message)[:240],
        )


def _printer_group(printer_uri: str, printer_up_time: int = 1) -> bytes:
    return _encode_group(
        TAG_PRINTER_ATTRIBUTES,
        [
            (VALUE_URI, "printer-uri-supported", printer_uri),
            (VALUE_KEYWORD, "uri-authentication-supported", "none"),
            (VALUE_KEYWORD, "uri-security-supported", "none"),
            (VALUE_CHARSET, "charset-configured", "utf-8"),
            (VALUE_CHARSET, "charset-supported", "utf-8"),
            (VALUE_LANGUAGE, "natural-language-configured", "en"),
            (VALUE_LANGUAGE, "generated-natural-language-supported", "en"),
            (VALUE_NAME, "printer-name", WINDOWS_PRINTER_NAME),
            (VALUE_TEXT, "printer-info", "Hive IPP Bridge"),
            (VALUE_TEXT, "printer-location", "Local computer"),
            (VALUE_TEXT, "printer-make-and-model", "Hive IPP Bridge localhost IPP endpoint"),
            (VALUE_URI, "printer-uuid", f"urn:uuid:{IPP_PRINTER_UUID}"),
            (VALUE_ENUM, "printer-state", 3),
            (VALUE_KEYWORD, "printer-state-reasons", "none"),
            (VALUE_TEXT, "printer-state-message", "Ready"),
            (VALUE_INTEGER, "printer-up-time", max(1, printer_up_time)),
            (VALUE_BOOLEAN, "printer-is-accepting-jobs", True),
            (VALUE_INTEGER, "queued-job-count", 0),
            (VALUE_KEYWORD, "ipp-versions-supported", "1.1"),
            (VALUE_KEYWORD, "ipp-versions-supported", "2.0"),
            (VALUE_KEYWORD, "pdl-override-supported", "attempted"),
            (VALUE_KEYWORD, "compression-supported", "none"),
            (VALUE_BOOLEAN, "multiple-document-jobs-supported", False),
            (VALUE_MIMETYPE, "document-format-supported", "application/pdf"),
            (VALUE_MIMETYPE, "document-format-default", "application/pdf"),
            (VALUE_KEYWORD, "job-creation-attributes-supported", "copies"),
            (VALUE_KEYWORD, "job-creation-attributes-supported", "document-format"),
            (VALUE_KEYWORD, "job-creation-attributes-supported", "document-name"),
            (VALUE_KEYWORD, "job-creation-attributes-supported", "job-name"),
            (VALUE_KEYWORD, "job-creation-attributes-supported", "media"),
            (VALUE_KEYWORD, "job-creation-attributes-supported", "print-color-mode"),
            (VALUE_KEYWORD, "job-creation-attributes-supported", "sides"),
            (VALUE_KEYWORD, "media-supported", "iso_a4_210x297mm"),
            (VALUE_KEYWORD, "media-supported", "na_letter_8.5x11in"),
            (VALUE_KEYWORD, "media-supported", "na_legal_8.5x14in"),
            (VALUE_KEYWORD, "media-ready", "iso_a4_210x297mm"),
            (VALUE_KEYWORD, "media-ready", "na_letter_8.5x11in"),
            (VALUE_KEYWORD, "media-default", "na_letter_8.5x11in"),
            (VALUE_KEYWORD, "sides-supported", "one-sided"),
            (VALUE_KEYWORD, "sides-supported", "two-sided-long-edge"),
            (VALUE_KEYWORD, "sides-supported", "two-sided-short-edge"),
            (VALUE_KEYWORD, "sides-default", "one-sided"),
            (VALUE_INTEGER, "copies-default", 1),
            (VALUE_RANGE, "copies-supported", (1, 100)),
            (VALUE_KEYWORD, "print-color-mode-supported", "color"),
            (VALUE_KEYWORD, "print-color-mode-supported", "monochrome"),
            (VALUE_KEYWORD, "print-color-mode-default", "color"),
            (VALUE_BOOLEAN, "color-supported", True),
            (VALUE_ENUM, "operations-supported", OP_PRINT_JOB),
            (VALUE_ENUM, "operations-supported", OP_VALIDATE_JOB),
            (VALUE_ENUM, "operations-supported", OP_CREATE_JOB),
            (VALUE_ENUM, "operations-supported", OP_SEND_DOCUMENT),
            (VALUE_ENUM, "operations-supported", OP_CANCEL_JOB),
            (VALUE_ENUM, "operations-supported", OP_GET_JOB_ATTRIBUTES),
            (VALUE_ENUM, "operations-supported", OP_GET_JOBS),
            (VALUE_ENUM, "operations-supported", OP_GET_PRINTER_ATTRIBUTES),
        ],
    )


def _job_group(job: Job) -> bytes:
    printer_uri = windows_user_printer_uri(job.route_token)
    return _encode_group(
        TAG_JOB_ATTRIBUTES,
        [
            (VALUE_INTEGER, "job-id", job.job_id),
            (VALUE_URI, "job-uri", f"{printer_uri}/job-{job.job_id}"),
            (VALUE_ENUM, "job-state", job.state),
            (VALUE_KEYWORD, "job-state-reasons", job.reason),
            (VALUE_NAME, "job-name", job.name),
        ],
    )


class IppRequestHandler(http.server.BaseHTTPRequestHandler):
    server: "IppHttpServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # The request path contains a per-user bearer route and must not be logged.
        self.server.logger.info("IPP request completed")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parts = urllib.parse.urlsplit(self.path).path.split("/")
        if len(parts) != 4 or parts[1] != "ipp" or not parts[2] or parts[3] != "print":
            self.send_error(404)
            return
        route_token = parts[2]
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > MAX_DOCUMENT_BYTES + 64 * 1024:
                raise IppProtocolError("IPP request body is too large")
            request = parse_request(self.rfile.read(length))
            response = self.server.process(request, route_token)
            self.send_response(200)
            self.send_header("Content-Type", "application/ipp")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        except IppProtocolError as error:
            self.server.logger.warning("invalid IPP request: %s", error)
            response = encode_response(
                IppRequest((1, 1), 0, 0, {}, b""), STATUS_CLIENT_BAD_REQUEST
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/ipp")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        except Exception:
            self.server.logger.exception("IPP request failed")
            self.send_error(500)


class IppHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, submitter: Callable[[bytes, dict[str, Any], str], tuple[bool, str]], logger: logging.Logger):
        self.logger = logger
        self.started_at = time.monotonic()
        self.store = JobStore(submitter, logger)
        super().__init__((IPP_HOST, IPP_PORT), IppRequestHandler)

    def process(self, request: IppRequest, route_token: str) -> bytes:
        if request.operation == OP_GET_PRINTER_ATTRIBUTES:
            uptime = max(1, int(time.monotonic() - self.started_at))
            printer_uri = windows_user_printer_uri(route_token)
            return encode_response(request, STATUS_OK, [_printer_group(printer_uri, uptime)])
        if request.operation == OP_VALIDATE_JOB:
            if request.first("document-format") not in {None, *ACCEPTED_DOCUMENT_FORMATS}:
                return encode_response(request, STATUS_CLIENT_DOCUMENT_FORMAT)
            return encode_response(request, STATUS_OK)
        if request.operation == OP_PRINT_JOB:
            if request.first("document-format") not in {None, *ACCEPTED_DOCUMENT_FORMATS}:
                return encode_response(request, STATUS_CLIENT_DOCUMENT_FORMAT)
            job = self.store.create(request, route_token)
            try:
                self.store.accept(job, request.data)
            except IppProtocolError:
                return encode_response(request, STATUS_CLIENT_DOCUMENT_FORMAT, [_job_group(job)])
            return encode_response(request, STATUS_OK, [_job_group(job)])
        if request.operation == OP_CREATE_JOB:
            job = self.store.create(request, route_token)
            return encode_response(request, STATUS_OK, [_job_group(job)])
        if request.operation == OP_SEND_DOCUMENT:
            job_id = request.first("job-id")
            if not isinstance(job_id, int):
                return encode_response(request, STATUS_CLIENT_BAD_REQUEST)
            job = self.store.get(job_id, route_token)
            if job is None:
                return encode_response(request, STATUS_CLIENT_NOT_FOUND)
            try:
                self.store.accept(job, request.data, last_document=bool(request.first("last-document", True)))
            except IppProtocolError:
                return encode_response(request, STATUS_CLIENT_DOCUMENT_FORMAT, [_job_group(job)])
            return encode_response(request, STATUS_OK, [_job_group(job)])
        if request.operation == OP_GET_JOB_ATTRIBUTES:
            job_id = request.first("job-id")
            job = self.store.get(job_id, route_token) if isinstance(job_id, int) else None
            return encode_response(request, STATUS_OK if job else STATUS_CLIENT_NOT_FOUND, [_job_group(job)] if job else None)
        if request.operation == OP_GET_JOBS:
            with self.store.lock:
                groups = [
                    _job_group(job)
                    for job in self.store.jobs.values()
                    if job.route_token == route_token
                ]
            return encode_response(request, STATUS_OK, groups)
        if request.operation == OP_CANCEL_JOB:
            job_id = request.first("job-id")
            job = self.store.get(job_id, route_token) if isinstance(job_id, int) else None
            if job is None:
                return encode_response(request, STATUS_CLIENT_NOT_FOUND)
            with self.store.lock:
                job.state = 7
                job.reason = "job canceled"
            return encode_response(request, STATUS_OK, [_job_group(job)])
        return encode_response(request, STATUS_CLIENT_BAD_REQUEST)


class IppServer:
    """Threaded listener with explicit readiness and shutdown lifecycle."""

    def __init__(self, submitter: Callable[[bytes, dict[str, Any], str], tuple[bool, str]], logger: logging.Logger):
        self.httpd = IppHttpServer(submitter, logger)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="hive-ipp-listener", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)

    @property
    def ready(self) -> bool:
        return self.thread.is_alive()

    def last_job_status(self) -> dict[str, object] | None:
        with self.httpd.store.lock:
            return dict(self.httpd.store.last_job_status) if self.httpd.store.last_job_status else None
