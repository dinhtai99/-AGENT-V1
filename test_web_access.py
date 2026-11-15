"""Checks for the passwordless access boundary used before public sharing."""

import tempfile
import unittest
from pathlib import Path

from web_access import COOKIE_NAME, WebAccess


class TestWebAccess(unittest.TestCase):
    def test_key_is_private_and_rotation_revokes_old_link(self):
        with tempfile.TemporaryDirectory() as temp:
            gate = WebAccess(Path(temp))
            old_key = gate.current_token()
            self.assertGreaterEqual(len(old_key), 32)
            self.assertTrue(gate.from_cookie(f"{COOKIE_NAME}={old_key}"))
            self.assertFalse(gate.from_cookie(f"{COOKIE_NAME}=invalid"))

            new_key = gate.rotate()
            self.assertNotEqual(new_key, old_key)
            self.assertFalse(gate.from_cookie(f"{COOKIE_NAME}={old_key}"))
            self.assertTrue(gate.from_cookie(f"{COOKIE_NAME}={new_key}"))
            self.assertEqual(WebAccess(Path(temp)).current_token(), new_key)


if __name__ == "__main__":
    unittest.main()
