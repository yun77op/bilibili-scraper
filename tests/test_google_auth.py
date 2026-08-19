"""Google 登录：按 google_sub 查找或建号，以及用户名冲突处理。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auth
import db


class GoogleAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        db.init_db()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_username_from_google_prefers_name(self):
        self.assertEqual(auth.username_from_google("Alice", "alice@gmail.com", "123"), "Alice")

    def test_username_from_google_falls_back_to_email(self):
        self.assertEqual(auth.username_from_google("", "bob.smith@gmail.com", "123"), "bob.smith")

    def test_username_from_google_strips_forbidden_chars(self):
        self.assertEqual(auth.username_from_google("a/b:c", "", "123"), "abc")

    def test_first_google_user_becomes_admin(self):
        ok, msg, user = auth.find_or_create_google_user(
            sub="gid-1", email="first@gmail.com", name="First",
        )
        self.assertTrue(ok, msg)
        self.assertIsNotNone(user)
        self.assertTrue(user["is_admin"])
        self.assertEqual(user["username"], "First")
        self.assertEqual(user["email"], "first@gmail.com")
        self.assertEqual(user["google_sub"], "gid-1")
        self.assertEqual(user["password_hash"], "")

    def test_second_google_user_not_admin(self):
        auth.find_or_create_google_user(sub="gid-1", email="a@gmail.com", name="A")
        ok, _, user = auth.find_or_create_google_user(
            sub="gid-2", email="b@gmail.com", name="B",
        )
        self.assertTrue(ok)
        self.assertFalse(user["is_admin"])

    def test_same_sub_returns_existing_user(self):
        _, _, first = auth.find_or_create_google_user(sub="gid-1", name="A")
        ok, _, again = auth.find_or_create_google_user(sub="gid-1", name="Changed")
        self.assertTrue(ok)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["username"], "A")

    def test_updates_email_on_subsequent_login(self):
        auth.find_or_create_google_user(sub="gid-1", email="old@gmail.com", name="A")
        _, _, user = auth.find_or_create_google_user(
            sub="gid-1", email="new@gmail.com", name="A",
        )
        self.assertEqual(user["email"], "new@gmail.com")

    def test_disabled_google_user_rejected(self):
        _, _, user = auth.find_or_create_google_user(sub="gid-1", name="A")
        db.update_user(user["id"], is_active=False)
        ok, msg, again = auth.find_or_create_google_user(sub="gid-1", name="A")
        self.assertFalse(ok)
        self.assertIsNone(again)
        self.assertIn("禁用", msg)

    def test_username_collision_gets_suffix(self):
        db.create_user(user_id="u1", username="Alice", password_hash="x")
        ok, _, user = auth.find_or_create_google_user(
            sub="gid-1", email="a@gmail.com", name="Alice",
        )
        self.assertTrue(ok)
        self.assertEqual(user["username"], "Alice-2")

    def test_password_login_blocked_for_google_only_user(self):
        auth.find_or_create_google_user(sub="gid-1", name="Alice")
        ok, msg = auth.login("Alice", "whatever")
        self.assertFalse(ok)
        self.assertIn("Google", msg)

    def test_google_login_enabled_with_env_secrets(self):
        missing = Path(self.tmp.name) / "missing.json"
        with mock.patch.dict(
            os.environ,
            {
                "GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com",
                "GOOGLE_CLIENT_SECRET": "s3cret",
                "GOOGLE_CREDENTIALS_PATH": str(missing),
                "GDRIVE_CREDENTIALS_PATH": str(missing),
            },
        ):
            self.assertTrue(auth.google_login_enabled())

    def test_google_login_disabled_without_creds(self):
        missing = Path(self.tmp.name) / "missing.json"
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "GOOGLE_CLIENT_ID",
                "GOOGLE_CLIENT_SECRET",
                "GOOGLE_CREDENTIALS_PATH",
                "GDRIVE_CREDENTIALS_PATH",
            )
        }
        env["GOOGLE_CREDENTIALS_PATH"] = str(missing)
        env["GDRIVE_CREDENTIALS_PATH"] = str(missing)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(auth.google_login_enabled())


if __name__ == "__main__":
    unittest.main()
