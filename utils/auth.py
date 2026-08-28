"""Authentication helpers and decorators."""
import hashlib
from functools import wraps
import time

import bcrypt
from flask import jsonify, redirect, request, session

_login_failures: dict = {}
LOGIN_FAIL_MAX = 5
LOGIN_LOCK_SECONDS = 900


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    if stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"):
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    if len(stored_hash) == 64 and all(c in "0123456789abcdef" for c in stored_hash.lower()):
        old_hash = hashlib.sha256(password.encode()).hexdigest()
        return old_hash == stored_hash
    return False


def check_login_limit(ip: str):
    now = time.time()
    if ip in _login_failures:
        count, last_time = _login_failures[ip]
        if count >= LOGIN_FAIL_MAX:
            elapsed = now - last_time
            if elapsed < LOGIN_LOCK_SECONDS:
                return False, int(LOGIN_LOCK_SECONDS - elapsed)
            _login_failures.pop(ip, None)
            return True, 0
    return True, 0


def record_login_failure(ip: str):
    now = time.time()
    if ip in _login_failures:
        count, _ = _login_failures[ip]
        _login_failures[ip] = [count + 1, now]
    else:
        _login_failures[ip] = [1, now]


def reset_login_failures(ip: str):
    _login_failures.pop(ip, None)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "未登录，请先登录后台"}), 401
            return redirect("/login")
        return f(*args, **kwargs)

    return decorated
