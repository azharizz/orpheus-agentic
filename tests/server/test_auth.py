import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

from orpheus.server import auth


class AuthChecks(unittest.TestCase):
    def test_cookie_round_trip_and_rotation(self):
        handler = SimpleNamespace(headers=Message())
        with patch.object(auth.config, "RUNTIME_MODE", "cloud_run"), patch.object(
            auth.config, "OWNER_SECRET", "test-secret"
        ):
            first = auth.resolve(handler)
            self.assertEqual(len(first), 32)
            self.assertTrue(handler.owner_cookie)
            cookie = handler.owner_cookie
            handler.headers["Cookie"] = f"{auth.COOKIE}={cookie}"
            second = auth.resolve(handler)
        self.assertEqual(first, second)
        self.assertIsNone(handler.owner_cookie)

    def test_local_mode_is_stable_without_secret(self):
        handler = SimpleNamespace(headers=Message())
        with patch.object(auth.config, "RUNTIME_MODE", "local"), patch.object(
            auth.config, "OWNER_SECRET", ""
        ):
            self.assertEqual(auth.resolve(handler), "local")
            self.assertIsNone(handler.owner_cookie)


if __name__ == "__main__":
    unittest.main()
