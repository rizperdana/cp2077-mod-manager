"""Stdlib unittest suite for the nexus-mcp-plus free-download engine.

Run from repo root:  python3 -m unittest discover -s nexus_mcp_plus/tests
(or `python3 -m unittest discover` from the repo root — both pass).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# make the package importable when run from the repo root
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nexus_mcp_plus import free_download as fd  # noqa: E402


class TestNxmparsing(unittest.TestCase):
    def test_popup_html_nxm_shape(self):
        """The DownloadPopUp HTML fallback shape (SahiDemon/NCD regex family)."""
        html = ('var url = "nxm://cyberpunk2077/mods/107/files/168615'
                '?key=abc123DEF&amp;expires=1767225600&amp;user_id=42";')
        m = fd.NXM_RE.search(html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "abc123DEF")
        self.assertEqual(m.group(2), "1767225600")

    def test_dmodman_plain_url_shape(self):
        """The dandels/dmodman NxmUrl shape (plain & separators)."""
        u = ("nxm://skyrimspecialedition/mods/12604/files/493229"
             "?key=K&expires=1")
        m = fd.NXM_RE.search(u)
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1), m.group(2)), ("K", "1"))

    def test_rejects_keyless(self):
        self.assertIsNone(fd.NXM_RE.search("nxm://cyberpunk2077/mods/1/files/2"))
        self.assertIsNone(fd.NXM_RE.search("https://example.com?key=x&expires=1"))


class TestRedaction(unittest.TestCase):
    def test_key_redacted_expires_kept(self):
        u = ("nxm://cyberpunk2077/mods/107/files/168615"
             "?key=SECRETVAL&expires=1767225600&user_id=42")
        r = fd.redact_nxm(u)
        self.assertNotIn("SECRETVAL", r)
        self.assertIn("key=***", r)
        self.assertIn("expires=1767225600", r)

    def test_redact_idempotent(self):
        u = "nxm://g/m/1/f/2?key=S&expires=1"
        self.assertEqual(fd.redact_nxm(fd.redact_nxm(u)),
                         fd.redact_nxm(u))

    def test_no_key_no_change(self):
        u = "https://localhost/a/b.zip"
        self.assertEqual(fd.redact_nxm(u), u)


class TestSessionStatusShape(unittest.TestCase):
    def test_status_json_shape_no_sessions(self):
        with mock.patch.object(fd, "read_firefox_cookies", return_value={}), \
             mock.patch.object(fd, "read_chromium_cookies", return_value={}):
            st = fd.session_status()
        self.assertEqual(
            sorted(st), ["api_key_present", "chromium_session",
                         "firefox_session", "hint", "premium"])
        self.assertIsInstance(st["api_key_present"], bool)
        self.assertIsInstance(st["firefox_session"], bool)
        self.assertIsInstance(st["chromium_session"], bool)
        self.assertIn("log into", st["hint"])
        self.assertIn("3buotdx0.default-release", st["hint"])

    def test_status_with_firefox_session(self):
        with mock.patch.object(fd, "read_firefox_cookies",
                               return_value={"nexusmods_session": "S"}), \
             mock.patch.object(fd, "read_chromium_cookies", return_value={}):
            st = fd.session_status()
        self.assertTrue(st["firefox_session"])
        self.assertFalse(st["chromium_session"])
        self.assertIn("ready", st["hint"])


class TestCleanErrorPath(unittest.TestCase):
    def test_main_exits_one_clean_sentence_no_session(self):
        with mock.patch.object(fd, "session_cookies", return_value={}), \
             mock.patch("sys.argv", ["nexus_dl.py", "cyberpunk2077", "107",
                                     "168615", "/tmp/x"]):
            with self.assertRaises(SystemExit) as cm:
                fd.main()
        self.assertEqual(cm.exception.code, f"Error: {fd.NO_SESSION_HINT}")
        self.assertNotIn("Traceback", str(cm.exception.code))


    def test_error_sentence_naming_fix(self):
        try:
            from nexus_mcp_plus.server import _err
        except ModuleNotFoundError:  # upstream not importable in this env
            self.skipTest("nexus_mcp (upstream) not installed")
        out = _err(fd.FreeDownloadError(fd.NO_SESSION_HINT))
        self.assertIn("log into", out)
        self.assertIn("3buotdx0.default-release", out)
        self.assertTrue(out.endswith("."))


class TestDownloadTempCleanup(unittest.TestCase):
    def test_part_file_removed_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)
            with mock.patch.object(fd, "DOWNLOAD_TIMEOUT", 5), \
                 mock.patch.object(
                     fd.urllib.request, "urlopen",
                     side_effect=OSError("connection reset")):
                with self.assertRaises(fd.FreeDownloadError):
                    fd.download("https://localhost/a/f.zip", str(dest))
            self.assertEqual(list(dest.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
