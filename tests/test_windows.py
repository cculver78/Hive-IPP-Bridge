from __future__ import annotations

import logging
import base64
import socket
import struct
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from hive_ipp_bridge import cli, enrollment
from hive_ipp_bridge.config import WINDOWS_IPP_URL
from hive_ipp_bridge.windows import credentials as windows_credentials
from hive_ipp_bridge.windows import ipc, ipp_server, platform_cli, printer, service as windows_service
from hive_ipp_bridge.windows.credentials import CredentialVault
from hive_ipp_bridge.windows.logging import SecretRedactionFilter


class WindowsSupportTests(unittest.TestCase):
    def test_platform_dispatches_to_windows_cli(self):
        with patch.object(cli.sys, "platform", "win32"), patch(
            "hive_ipp_bridge.windows.platform_cli.main", return_value=7
        ) as windows_main:
            self.assertEqual(cli.main(), 7)
            windows_main.assert_called_once_with()

    def test_ipp_parser_preserves_document_bytes(self):
        request = (
            struct.pack(">BBHI", 1, 1, ipp_server.OP_PRINT_JOB, 42)
            + bytes([ipp_server.TAG_OPERATION_ATTRIBUTES])
            + bytes([ipp_server.VALUE_URI])
            + struct.pack(">H", 11)
            + b"printer-uri"
            + struct.pack(">H", len(WINDOWS_IPP_URL))
            + WINDOWS_IPP_URL.encode()
            + bytes([ipp_server.TAG_END])
            + b"%PDF-test"
        )
        parsed = ipp_server.parse_request(request)
        self.assertEqual(parsed.operation, ipp_server.OP_PRINT_JOB)
        self.assertEqual(parsed.request_id, 42)
        self.assertEqual(parsed.first("printer-uri"), WINDOWS_IPP_URL)
        self.assertEqual(parsed.data, b"%PDF-test")

    def test_printer_attributes_are_valid_for_directed_discovery(self):
        request = ipp_server.IppRequest(
            (2, 0), ipp_server.OP_GET_PRINTER_ATTRIBUTES, 7, {}, b""
        )
        response = ipp_server.encode_response(
            request,
            ipp_server.STATUS_OK,
            [ipp_server._printer_group("ipp://127.0.0.1:8631/ipp/test-route/print")],
        )
        parsed = ipp_server.parse_request(response)

        required = {
            "charset-configured",
            "charset-supported",
            "document-format-default",
            "document-format-supported",
            "generated-natural-language-supported",
            "natural-language-configured",
            "operations-supported",
            "pdl-override-supported",
            "printer-is-accepting-jobs",
            "printer-state",
            "printer-state-reasons",
            "printer-up-time",
            "printer-uri-supported",
            "uri-authentication-supported",
            "uri-security-supported",
        }
        self.assertTrue(required.issubset(parsed.attributes))
        self.assertIn("2.0", parsed.attributes["ipp-versions-supported"])
        self.assertEqual(parsed.first("document-format-supported"), "application/pdf")
        uuid.UUID(parsed.first("printer-uuid").removeprefix("urn:uuid:"))

    def test_ipp_job_store_submits_pdf_and_tracks_completion(self):
        submitted: list[bytes] = []
        logger = logging.getLogger("windows-test")
        store = ipp_server.JobStore(
            lambda data, _metadata, _route: (submitted.append(data) or True, "accepted"),
            logger,
        )
        request = ipp_server.IppRequest((1, 1), ipp_server.OP_PRINT_JOB, 1, {"job-name": ["sample"]}, b"")
        job = store.create(request)
        store.accept(job, b"%PDF-test")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and job.state == 5:
            time.sleep(0.01)
        self.assertEqual(submitted, [b"%PDF-test"])
        self.assertEqual(job.state, 9)

    def test_ipp_job_store_accepts_a_pdf_split_across_send_document_chunks(self):
        submitted: list[bytes] = []
        store = ipp_server.JobStore(
            lambda data, _metadata, _route: (submitted.append(data) or True, "accepted"),
            logging.getLogger("windows-test"),
        )
        request = ipp_server.IppRequest((1, 1), ipp_server.OP_CREATE_JOB, 1, {}, b"")
        job = store.create(request)
        store.accept(job, b"%PDF-first", last_document=False)
        store.accept(job, b"-second", last_document=True)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and job.state == 5:
            time.sleep(0.01)
        self.assertEqual(submitted, [b"%PDF-first-second"])
        self.assertEqual(job.state, 9)

    def test_listener_accepts_octet_stream_metadata_for_a_pdf_payload(self):
        with patch.object(ipp_server, "IPP_HOST", "127.0.0.1"), patch.object(
            ipp_server, "IPP_PORT", 0
        ):
            try:
                server = ipp_server.IppHttpServer(
                    lambda _data, _metadata, _route: (True, "accepted"),
                    logging.getLogger("windows-test"),
                )
            except PermissionError:
                self.skipTest("sandbox does not permit local socket binding")
            try:
                request = ipp_server.IppRequest(
                    (1, 1), ipp_server.OP_VALIDATE_JOB, 1,
                    {"document-format": ["application/octet-stream"]}, b"",
                )
                response = server.process(request, "route")
                self.assertEqual(struct.unpack(">H", response[2:4])[0], ipp_server.STATUS_OK)
            finally:
                server.server_close()

    def test_listener_reports_ready_and_detects_port_conflict(self):
        try:
            occupied = socket.socket()
        except PermissionError:
            self.skipTest("sandbox does not permit local socket binding")
        with occupied:
            try:
                occupied.bind(("127.0.0.1", 0))
            except PermissionError:
                self.skipTest("sandbox does not permit local socket binding")
            host, port = occupied.getsockname()
            with patch.object(ipp_server, "IPP_HOST", host), patch.object(ipp_server, "IPP_PORT", port):
                with self.assertRaises(OSError):
                    ipp_server.IppServer(
                        lambda _data, _metadata, _route: (True, "ok"),
                        logging.getLogger("windows-test"),
                    )

        try:
            probe = socket.socket()
        except PermissionError:
            self.skipTest("sandbox does not permit local socket binding")
        with probe:
            try:
                probe.bind(("127.0.0.1", 0))
            except PermissionError:
                self.skipTest("sandbox does not permit local socket binding")
            host, port = probe.getsockname()
        with patch.object(ipp_server, "IPP_HOST", host), patch.object(ipp_server, "IPP_PORT", port):
            server = ipp_server.IppServer(
                lambda _data, _metadata, _route: (True, "ok"),
                logging.getLogger("windows-test"),
            )
            try:
                server.start()
                self.assertTrue(server.ready)
            finally:
                server.shutdown()

    def test_printer_matching_requires_driver_and_expected_uri(self):
        good = printer.PrinterInfo(
            printer.WINDOWS_PRINTER_NAME,
            printer.EXPECTED_DRIVER,
            "port",
            (WINDOWS_IPP_URL,),
        )
        bad_uri = printer.PrinterInfo("Hive IPP Bridge", printer.EXPECTED_DRIVER, "port", ("http://127.0.0.1:1/ipp/print",))
        bad_driver = printer.PrinterInfo("Hive IPP Bridge", "PaperCut Driver", "port", (WINDOWS_IPP_URL,))
        self.assertTrue(good.matches_bridge)
        self.assertFalse(bad_uri.matches_bridge)
        self.assertFalse(bad_driver.matches_bridge)
        self.assertIn("no managed localhost IPP URI", bad_uri.bridge_mismatch_reason)
        self.assertIn("driver is 'PaperCut Driver'", bad_driver.bridge_mismatch_reason)

    def test_printer_matching_does_not_accept_wsd_as_a_ready_transport(self):
        info = printer.PrinterInfo(
            "Hive IPP Bridge (User-12345678)",
            printer.EXPECTED_DRIVER,
            "WSD-example",
            (),
            "8df4bf96-6c39-4b2f-a2a6-863186318631",
        )
        self.assertFalse(info.matches_profile(info.name, "http://127.0.0.1:8631/ipp/private/print"))

    def test_windows_setup_does_not_prompt_when_service_has_usable_credentials(self):
        with patch.object(platform_cli.os, "name", "nt"), \
                patch.object(platform_cli.service, "service_managed", return_value=True), \
                patch.object(platform_cli, "wait_for_listener", return_value=True), \
                patch.object(
                    platform_cli.ControlClient,
                    "request",
                    side_effect=[
                        {"ok": True, "credentials": "usable", "route_token": "user-route"},
                        {"ok": True, "printer_name": "Hive IPP Bridge (User-12345678)"},
                    ],
                ), \
                patch.object(platform_cli, "_enroll") as enroll:
            self.assertEqual(platform_cli.setup_command(object()), 0)
            enroll.assert_not_called()

    def test_uac_helper_encodes_paths_instead_of_passing_command_text(self):
        completed = type("Completed", (), {"returncode": 0})()
        with patch.object(platform_cli.os, "name", "nt"), \
                patch.object(platform_cli.service, "is_admin", return_value=False), \
                patch.object(platform_cli.subprocess, "run", return_value=completed) as run:
            self.assertEqual(platform_cli.run_admin_operation("install-services"), 0)

        command = run.call_args.args[0]
        self.assertIn("-EncodedCommand", command)
        encoded = command[command.index("-EncodedCommand") + 1]
        decoded = base64.b64decode(encoded).decode("utf-16le")
        self.assertIn("Start-Process", decoded)
        self.assertIn("--admin-operation", decoded)
        self.assertNotIn("-Command", command)

    def test_machine_removal_refuses_printer_mismatch(self):
        bad = printer.PrinterInfo("Hive IPP Bridge", "Other driver", "port", (WINDOWS_IPP_URL,))
        with patch.object(printer, "managed_printers", return_value=[bad]), \
                patch.object(printer, "_powershell") as powershell:
            with self.assertRaises(printer.PrinterError):
                printer.remove_all_managed_printers()
            powershell.assert_not_called()

    def test_service_removal_uses_the_windows_delete_access_right(self):
        delete_access = 0x00010000
        requested_access: list[int] = []
        win32con = types.ModuleType("win32con")
        win32con.DELETE = delete_access
        fake_service = types.SimpleNamespace(SC_MANAGER_CONNECT=1)
        fake_service.OpenSCManager = lambda *_args: object()

        def open_service(_scm, _name, access):
            requested_access.append(access)
            return object()

        fake_service.OpenService = open_service
        fake_service.DeleteService = lambda _handle: None
        fake_service.CloseServiceHandle = lambda _handle: None

        with patch.dict(sys.modules, {"win32con": win32con}), \
                patch.object(windows_service, "is_admin", return_value=True), \
                patch.object(windows_service, "_win32_modules", return_value=(None, None, None, fake_service, None)), \
                patch.object(windows_service, "service_installed", return_value=True), \
                patch.object(windows_service, "service_managed", return_value=True), \
                patch.object(windows_service, "stop_service"):
            windows_service.remove_service()

        self.assertEqual(requested_access, [delete_access, delete_access])

    def test_printer_inspection_handles_missing_queue(self):
        with patch("hive_ipp_bridge.windows.printer._powershell", side_effect=printer.PrinterError("Windows printer command failed (exit code 3)")):
            self.assertIsNone(printer.inspect_printer())

    def test_vault_rejects_incomplete_credentials_before_dpapi(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = CredentialVault(Path(directory) / "credentials.dpapi")
            with self.assertRaisesRegex(Exception, "incomplete"):
                vault.save(enrollment.Credentials("", "client", "org"))

    def test_multiuser_vault_keeps_profiles_and_routes_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = CredentialVault(root / "credentials.dpapi")
            with patch.object(windows_credentials, "protect", side_effect=lambda value: value), \
                    patch.object(windows_credentials, "unprotect", side_effect=lambda value: value), \
                    patch.object(windows_credentials, "ensure_data_root", return_value=root), \
                    patch.object(windows_credentials, "_ensure_windows_acl"), \
                    patch.object(windows_credentials, "route_registry_path", return_value=root / "routes.json"):
                first = enrollment.Credentials("jwt-a", "client-a", "org-a")
                second = enrollment.Credentials("jwt-b", "client-b", "org-b")
                route_a = vault.save_for_user("S-1-5-21-1001", first, "CLASSROOM\\alice")
                route_b = vault.save_for_user("S-1-5-21-1002", second, "CLASSROOM\\bob")

                self.assertNotEqual(route_a, route_b)
                self.assertEqual(vault.profile_for_user("S-1-5-21-1001")[0], first)
                self.assertEqual(vault.profile_for_token(route_b), ("S-1-5-21-1002", second))
                vault.clear_user("S-1-5-21-1001")
                with self.assertRaises(windows_credentials.CredentialError):
                    vault.profile_for_user("S-1-5-21-1001")
                self.assertEqual(vault.profile_for_user("S-1-5-21-1002")[0], second)

    def test_data_directory_acl_entries_are_inheritable(self):
        entries: list[tuple[int, int, object]] = []

        class FakeAcl:
            def AddAccessAllowedAceEx(self, _revision, flags, access, sid):
                entries.append((flags, access, sid))

        class FakeDescriptor:
            def SetSecurityDescriptorDacl(self, *_args):
                pass

        ntsecuritycon = types.ModuleType("ntsecuritycon")
        ntsecuritycon.FILE_ALL_ACCESS = 0x1F01FF
        ntsecuritycon.FILE_GENERIC_READ = 0x120089
        win32security = types.ModuleType("win32security")
        win32security.ACL_REVISION = 2
        win32security.OBJECT_INHERIT_ACE = 1
        win32security.CONTAINER_INHERIT_ACE = 2
        win32security.DACL_SECURITY_INFORMATION = 4
        win32security.ACL = lambda _size: FakeAcl()
        win32security.ConvertStringSidToSid = lambda value: value
        win32security.SECURITY_DESCRIPTOR = FakeDescriptor
        win32security.SetFileSecurity = lambda *_args: None

        directory = type(
            "DirectoryPath",
            (),
            {"is_dir": lambda self: True, "__str__": lambda self: r"C:\ProgramData\Hive IPP Bridge"},
        )()
        with patch.dict(
            sys.modules,
            {"ntsecuritycon": ntsecuritycon, "win32security": win32security},
        ), patch.object(windows_credentials.os, "name", "nt"):
            windows_credentials._ensure_windows_acl(directory)

        self.assertEqual(len(entries), 3)
        self.assertTrue(all(flags == 3 for flags, _access, _sid in entries))

    def test_provisioner_rejects_a_route_owned_by_another_sid(self):
        runtime = windows_service.ProvisionerRuntime.__new__(windows_service.ProvisionerRuntime)
        with patch.object(
            windows_service, "load_user_route", return_value=("alice-route", "CLASSROOM\\alice")
        ), patch.object(windows_service.printer, "ensure_printer") as ensure:
            response = runtime.handle_request(
                {"op": "ensure-printer", "route_token": "bob-route"}, "S-1-5-21-1001"
            )

        self.assertFalse(response["ok"])
        ensure.assert_not_called()

    def test_jobs_are_not_visible_across_private_routes(self):
        store = ipp_server.JobStore(
            lambda _data, _metadata, _route: (True, "accepted"), logging.getLogger("windows-test")
        )
        request = ipp_server.IppRequest((2, 0), ipp_server.OP_CREATE_JOB, 1, {}, b"")
        job = store.create(request, "alice-route")
        self.assertIs(store.get(job.job_id, "alice-route"), job)
        self.assertIsNone(store.get(job.job_id, "bob-route"))

    def test_control_messages_are_length_delimited(self):
        encoded = platform_cli  # keep platform module imported on Linux for dispatch coverage
        del encoded
        from hive_ipp_bridge.windows.ipc import (
            IpcError,
            decode_request,
            decode_response,
            encode_request,
            encode_response,
        )

        request = {"op": "status"}
        self.assertEqual(decode_request(encode_request(request)), request)
        response = {"ok": True, "credentials": "usable"}
        self.assertEqual(decode_response(encode_response(response)), response)
        with self.assertRaises(IpcError):
            decode_request(b"\x04\x00\x00\x00{}")
        with self.assertRaises(IpcError):
            decode_response(encode_response({"op": "status"}))

    def test_control_response_is_flushed_before_disconnect(self):
        from hive_ipp_bridge.windows.ipc import _write_response, decode_response

        class FakeWin32File:
            def __init__(self):
                self.events: list[object] = []

            def WriteFile(self, _handle, payload):
                self.events.append(payload)
                return 0, len(payload)

            def FlushFileBuffers(self, _handle):
                self.events.append("flush")

        api = FakeWin32File()
        _write_response(object(), api, {"ok": True})

        self.assertEqual(api.events[-1], "flush")
        self.assertEqual(decode_response(b"".join(api.events[:-1])), {"ok": True})

    def test_control_client_imports_restricted_pipe_write_access(self):
        response = ipc.encode_response({"ok": True})
        offset = 0
        requested_access: list[int] = []

        pywintypes = types.ModuleType("pywintypes")
        pywintypes.error = OSError
        ntsecuritycon = types.ModuleType("ntsecuritycon")
        ntsecuritycon.FILE_WRITE_DATA = 0x0002
        win32file = types.ModuleType("win32file")
        win32file.GENERIC_READ = 0x80000000
        win32file.OPEN_EXISTING = 3

        def create_file(_name, access, *_args):
            requested_access.append(access)
            return object()

        def read_file(_handle, size):
            nonlocal offset
            chunk = response[offset:offset + size]
            offset += len(chunk)
            return 0, chunk

        win32file.CreateFile = create_file
        win32file.WriteFile = lambda _handle, payload: (0, len(payload))
        win32file.ReadFile = read_file
        win32file.CloseHandle = lambda _handle: None
        modules = {
            "ntsecuritycon": ntsecuritycon,
            "pywintypes": pywintypes,
            "win32file": win32file,
        }
        with patch.dict(sys.modules, modules), patch.object(ipc.os, "name", "nt"):
            self.assertEqual(ipc.ControlClient().request({"op": "status"}), {"ok": True})

        self.assertEqual(
            requested_access,
            [win32file.GENERIC_READ | ntsecuritycon.FILE_WRITE_DATA],
        )

    def test_named_pipe_impersonation_uses_win32security(self):
        source = Path(ipc.__file__).read_text(encoding="utf-8")
        self.assertIn("win32security.ImpersonateNamedPipeClient(handle)", source)
        self.assertNotIn("win32pipe.ImpersonateNamedPipeClient(handle)", source)

    def test_logging_filter_rejects_sensitive_messages(self):
        filter_ = SecretRedactionFilter()
        safe = logging.LogRecord("test", logging.INFO, __file__, 1, "listener started", (), None)
        secret = logging.LogRecord("test", logging.INFO, __file__, 1, "token=%s", ("secret",), None)
        self.assertTrue(filter_.filter(safe))
        self.assertFalse(filter_.filter(secret))


if __name__ == "__main__":
    unittest.main()
