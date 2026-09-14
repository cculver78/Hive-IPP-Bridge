from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from hive_ipp_bridge import cli


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class CliTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch.object(cli, "secret_store", return_value=True)
    @patch.object(cli.enrollment, "claim")
    @patch.object(cli.getpass, "getpass", return_value="private-link")
    def test_enrollment_prompts_privately_and_stores_claimed_credentials(
        self, getpass, claim, store
    ):
        claim.return_value = cli.enrollment.Credentials("jwt", "client", "org")

        self.assertTrue(cli.enroll_credentials())

        getpass.assert_called_once()
        claim.assert_called_once_with("private-link")
        self.assertEqual(
            store.call_args_list,
            [unittest.mock.call("client-id", "client"),
             unittest.mock.call("org-id", "org"),
             unittest.mock.call("jwt", "jwt")],
        )

    @patch.dict(os.environ, {}, clear=True)
    @patch.object(cli, "secret_clear")
    @patch.object(cli, "secret_store", side_effect=(True, False))
    @patch.object(cli.enrollment, "claim")
    @patch.object(cli.getpass, "getpass", return_value="private-link")
    def test_enrollment_rolls_back_partial_keyring_write(
        self, _getpass, claim, _store, clear
    ):
        claim.return_value = cli.enrollment.Credentials("jwt", "client", "org")

        self.assertFalse(cli.enroll_credentials())

        clear.assert_called_once_with("client-id")

    def test_parser_exposes_document_for_test_command(self):
        args = cli.parser().parse_args(["test", "document.pdf"])
        self.assertEqual(args.document, Path("document.pdf"))
        self.assertIs(args.handler, cli.test_command)

    @patch.object(cli, "run")
    def test_queue_uri_extracts_device_uri(self, run):
        run.return_value = completed(stdout=f"device for {cli.QUEUE_NAME}: {cli.DEVICE_URI}\n")
        self.assertEqual(cli.queue_uri(), cli.DEVICE_URI)
    @patch.object(cli, "run")
    def test_queue_uri_accepts_an_empty_printer_list(self, run):
        run.return_value = completed(returncode=1, stderr="lpstat: No destinations added.\n")
        self.assertIsNone(cli.queue_uri())


    def test_service_quotes_adapter_path(self):
        unit = cli.service_text()
        self.assertIn('-c "', unit)
        self.assertIn('" "Hive IPP Bridge"', unit)

    @patch.object(cli.shutil, "which", return_value="/usr/bin/tool")
    @patch.object(cli, "queue_uri", return_value="ipp://example.test/other")
    def test_setup_refuses_conflicting_queue_before_keyring_write(self, _queue_uri, _which):
        with patch.object(cli, "secret_store") as store:
            self.assertEqual(cli.setup_command(unittest.mock.Mock()), 1)
            store.assert_not_called()


    @patch.object(cli, "require_commands", return_value=True)
    @patch.object(cli, "queue_uri", side_effect=(None, cli.DEVICE_URI))
    @patch.object(cli, "run", return_value=completed())
    def test_uninstall_removes_legacy_queue(self, run, _queue_uri, _requirements):
        self.assertEqual(cli.uninstall_command(argparse.Namespace(purge=False, full=False)), 0)
        self.assertIn(unittest.mock.call(["lpadmin", "-x", "PaperCut_Hive"]), run.call_args_list)

    @patch.object(cli, "require_commands", return_value=True)
    @patch.object(cli, "queue_uri", side_effect=cli.CupsError("scheduler unavailable"))
    def test_uninstall_reports_cups_failure(self, _queue_uri, _requirements):
        self.assertEqual(cli.uninstall_command(argparse.Namespace(purge=False, full=False)), 1)


    def test_full_remove_deletes_only_managed_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_dir = root / "lib"
            launcher = root / "hive-ipp-bridge"
            install_dir.mkdir()
            (install_dir / "cli.py").write_text("installed")
            launcher.write_text("HIVE_IPP_BRIDGE_LAUNCHER=1\n")
            with patch.object(cli, "INSTALL_DIR", install_dir), patch.object(cli, "LAUNCHER", launcher):
                self.assertTrue(cli.remove_user_program())
            self.assertFalse(install_dir.exists())
            self.assertFalse(launcher.exists())


if __name__ == "__main__":
    unittest.main()
