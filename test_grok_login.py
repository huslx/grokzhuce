"""离线检查：python3 -m unittest test_grok_login.py（不启动浏览器）。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_login import account_password, atomic_write, load_login_emails, read_records, save_results


class LoginStorageTest(unittest.TestCase):
    def test_failed_reset_passwords_are_not_used_for_login(self):
        records = {
            "failed@example.com": {"success": False, "password": "never-applied"},
            "ok@example.com": {"success": True, "password": "saved"},
            "manual@example.com": {"password": "known"},
        }
        with patch("grok_login.load_emails", return_value=[
            "FAILED@example.com", "ok@example.com", "manual@example.com", "env@example.com"
        ]):
            self.assertEqual(load_login_emails("unused.json", records), [
                "ok@example.com", "manual@example.com", "env@example.com"
            ])

    def test_credentials_and_safe_output(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "credentials.json"
            original = '{"A@example.com": {"password": " saved password ", "sso": "old"}}'
            credentials.write_text(original)
            records = read_records(credentials)
            with patch.dict(os.environ, {"ACCOUNT_PASSWORD": "fallback"}):
                self.assertEqual(account_password("A@example.com", records), " saved password ")
                self.assertEqual(account_password("b@example.com", records), "fallback")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    account_password("missing@example.com", records)
            output = Path(directory) / "login.json"
            results = {
                "a@example.com": {"success": True, "sso": "fresh"},
                "b@example.com": {"success": False, "sso": "stale"},
            }
            save_results(output, results)
            self.assertEqual(json.loads(output.read_text()), results)
            self.assertEqual(output.with_suffix(".txt").read_text(), "fresh\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with patch("grok_login.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    atomic_write(output, "broken")
            self.assertEqual(json.loads(output.read_text()), results)
            self.assertEqual(credentials.read_text(), original)


if __name__ == "__main__":
    unittest.main()
