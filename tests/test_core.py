#!/usr/bin/env python3
"""关键路径纯函数单测。

只测不依赖 DB/Redis/Flask 请求上下文的纯逻辑，保证重构 app.py 时不回归。
跑法: python3 -m pytest tests/test_core.py -q   或   python3 tests/test_core.py
"""
import os
import sys
import unittest

# app.py 在 import 期会读环境变量并可能连 DB，这里塞最小假值让它能 import
os.environ.setdefault("DB_PASSWORD", "x")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_USER", "test")
os.environ.setdefault("ADMIN_PASS", "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestPassword(unittest.TestCase):
    def _mod(self):
        import app
        return app

    def test_bcrypt_roundtrip(self):
        app = self._mod()
        h = app.hash_password("s3cret-pass")
        self.assertTrue(h.startswith("$2b$"))
        self.assertTrue(app.verify_password("s3cret-pass", h))
        self.assertFalse(app.verify_password("wrong", h))

    def test_legacy_sha256_verify(self):
        import hashlib
        app = self._mod()
        legacy = hashlib.sha256(b"old-pass").hexdigest()
        self.assertTrue(app.verify_password("old-pass", legacy))
        self.assertFalse(app.verify_password("nope", legacy))

    def test_empty_hash_rejected(self):
        app = self._mod()
        self.assertFalse(app.verify_password("anything", ""))


class TestRole(unittest.TestCase):
    def _mod(self):
        import app
        return app

    def test_norm_role(self):
        app = self._mod()
        self.assertEqual(app._norm_role("SuperAdmin"), "superadmin")
        self.assertEqual(app._norm_role("ADMIN"), "admin")
        self.assertEqual(app._norm_role(None), "user")
        self.assertEqual(app._norm_role("whatever"), "user")

    def test_staff_and_super(self):
        app = self._mod()
        self.assertTrue(app._is_staff_role("admin"))
        self.assertTrue(app._is_staff_role("superadmin"))
        self.assertFalse(app._is_staff_role("user"))
        self.assertTrue(app._is_superadmin_role("super"))
        self.assertFalse(app._is_superadmin_role("admin"))


class TestSafeNext(unittest.TestCase):
    def _mod(self):
        import app
        return app

    def test_blocks_open_redirect(self):
        app = self._mod()
        self.assertEqual(app._safe_next_url("https://evil.com"), "/")
        self.assertEqual(app._safe_next_url("//evil.com"), "/")
        self.assertEqual(app._safe_next_url("/login/x"), "/")
        self.assertEqual(app._safe_next_url(""), "/")

    def test_allows_internal(self):
        app = self._mod()
        self.assertEqual(app._safe_next_url("/admin"), "/admin")
        self.assertEqual(app._safe_next_url("/profile?x=1"), "/profile?x=1")


class TestCacheKey(unittest.TestCase):
    def _mod(self):
        import app
        return app

    def test_prefix_and_stability(self):
        app = self._mod()
        k1 = app._make_cache_key("resources", "q", 1)
        k2 = app._make_cache_key("resources", "q", 1)
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith("res_web:resources:"))
        k3 = app._make_cache_key("resources", "q", 2)
        self.assertNotEqual(k1, k3)


class TestJsonDefault(unittest.TestCase):
    def _mod(self):
        import app
        return app

    def test_datetime_and_decimal(self):
        import datetime as dt
        import decimal
        app = self._mod()
        self.assertEqual(app._json_default(dt.datetime(2026, 1, 2, 3, 4, 5)),
                         "2026-01-02T03:04:05")
        self.assertEqual(app._json_default(decimal.Decimal("1.5")), 1.5)
        self.assertEqual(app._json_default(b"abc"), "abc")


class TestDbSessionSemantics(unittest.TestCase):
    """db_session 的 commit/rollback/close 语义，用假连接验证，不打真库。"""

    def test_commit_on_success(self):
        import app

        class FakeCur:
            pass

        class FakeDb:
            def __init__(self):
                self.committed = False
                self.rolled = False
                self.closed = False

            def cursor(self):
                return FakeCur()

            def commit(self):
                self.committed = True

            def rollback(self):
                self.rolled = True

            def close(self):
                self.closed = True

        fake = FakeDb()
        orig = app.get_db
        app.get_db = lambda: fake
        try:
            with app.db_session() as cur:
                self.assertIsInstance(cur, FakeCur)
            self.assertTrue(fake.committed)
            self.assertFalse(fake.rolled)
            self.assertTrue(fake.closed)
        finally:
            app.get_db = orig

    def test_rollback_on_error(self):
        import app

        class FakeDb:
            def __init__(self):
                self.rolled = False
                self.closed = False

            def cursor(self):
                return object()

            def commit(self):
                pass

            def rollback(self):
                self.rolled = True

            def close(self):
                self.closed = True

        fake = FakeDb()
        orig = app.get_db
        app.get_db = lambda: fake
        try:
            with self.assertRaises(ValueError):
                with app.db_session():
                    raise ValueError("boom")
            self.assertTrue(fake.rolled)
            self.assertTrue(fake.closed)
        finally:
            app.get_db = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
