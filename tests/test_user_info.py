"""Tests for the IAP-derived user identity endpoint used by the React topbar."""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from server import DEFAULT_DISPLAY_NAME, app, resolve_user_identity


class TestUserInfo(unittest.TestCase):
    """Covers header parsing, local fallback, and the anonymous default."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_iap_headers_yield_username_before_at_sign(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCAL_USER_EMAIL", None)
            res = self.client.get(
                "/api/user-info",
                headers={
                    "X-Goog-Authenticated-User-Email": "accounts.google.com:simran.singh@example.com",
                    "X-Goog-Authenticated-User-ID": "accounts.google.com:1234567890",
                },
            )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["username"], "simran.singh")
        self.assertEqual(body["email"], "simran.singh@example.com")
        self.assertEqual(body["user_id"], "1234567890")
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["source"], "iap")
        self.assertEqual(res.headers.get("cache-control"), "no-store")

    def test_local_user_email_fallback_is_not_marked_authenticated(self) -> None:
        with mock.patch.dict(os.environ, {"LOCAL_USER_EMAIL": "dev.user@corp.example"}):
            res = self.client.get("/api/user-info")
        body = res.json()
        self.assertEqual(body["username"], "dev.user")
        self.assertFalse(body["authenticated"])
        self.assertEqual(body["source"], "local")

    def test_defaults_to_user_without_headers_or_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCAL_USER_EMAIL", None)
            res = self.client.get("/api/user-info")
        body = res.json()
        self.assertEqual(body["username"], DEFAULT_DISPLAY_NAME)
        self.assertEqual(body["email"], "")
        self.assertFalse(body["authenticated"])
        self.assertEqual(body["source"], "anonymous")

    def test_resolver_handles_prefixless_and_malformed_values(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCAL_USER_EMAIL", None)
            # Prefix is optional / case-insensitive; a value without '@' is used verbatim.
            identity = resolve_user_identity({"x-goog-authenticated-user-email": "ACCOUNTS.GOOGLE.COM:ops-bot"})
            self.assertEqual(identity["username"], "ops-bot")
            # Empty local part never produces an empty display name.
            identity = resolve_user_identity({"x-goog-authenticated-user-email": "@example.com"})
            self.assertEqual(identity["username"], DEFAULT_DISPLAY_NAME)

    def test_healthz_and_spa_serving_mode(self) -> None:
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

        with mock.patch.dict(os.environ, {"SERVE_REACT_SPA": "1"}):
            for path in (
                "/",
                "/guide",
                "/execution_dashboard/sess-1",
                "/visualize/sess-1",
                "/tests/sess-1",
            ):
                res = self.client.get(path)
                self.assertEqual(res.status_code, 200, path)
                self.assertIn('<div id="root"></div>', res.text)


if __name__ == "__main__":
    unittest.main()

