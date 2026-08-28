"""HTML page routes."""
from flask import Blueprint, jsonify, make_response, redirect, request, send_from_directory, session

from utils.auth import admin_required

pages_bp = Blueprint("pages", __name__)
@pages_bp.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        data = request.get_json() or request.form
        username = data.get("username", "").strip()
        password = data.get("password", "")
        client_ip = request.remote_addr
        # 检查登录限流
        allowed, remaining = check_login_limit(client_ip)
        if not allowed:
            return jsonify({"ok": False, "error": f"登录尝试过多，请{remaining}秒后再试"}), 429
        if not username or not password:
            return jsonify({"ok": False, "error": "请输入用户名和密码"}), 401
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT id, username, role, status, password FROM users WHERE username=%s", (username,))
            user = cur.fetchone()
        finally:
            db.close()
        if not user:
            record_login_failure(client_ip)
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        if user["status"] != 1:
            return jsonify({"ok": False, "error": "账号已被禁用"}), 401
        # 验证密码（兼容旧版 SHA256，自动迁移 bcrypt）
        pw_hash = password  # 直接传入原始密码给 verify_password
        stored_pw = user.get("password", "")
        if not verify_password(password, stored_pw):
            # 登录成功，自动迁移到 bcrypt
            if stored_pw and not stored_pw.startswith('$2b$') and not stored_pw.startswith('$2a$'):
                new_hash = hash_password(password)
                db2 = get_db()
                try:
                    db2.cursor().execute("UPDATE users SET password=%s WHERE id=%s", (new_hash, user["id"]))
                    db2.commit()
                except Exception:
                    pass
                finally:
                    db2.close()
            record_login_failure(client_ip)
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        session["is_admin"] = user["role"] == "admin"
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
        # 重新生成 session ID，防止会话固定攻击
        session.modified = True
        reset_login_failures(client_ip)
        # 更新最后登录时间
        db2 = get_db()
        try:
            db2.cursor().execute("UPDATE users SET last_login=NOW() WHERE id=%s", (user["id"],))
            db2.commit()
        finally:
            db2.close()
        if request.is_json:
            return jsonify({"ok": True, "role": user["role"]})
        return redirect("/admin" if user["role"] == "admin" else "/")
    resp = send_from_directory(".", "login.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp

@pages_bp.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@pages_bp.route("/")
def index():
    resp = send_from_directory(".", "index.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp

@pages_bp.route("/admin")
@admin_required
def admin_page():
    # Force no-cache with version-busting
    from flask import make_response
    with open("admin.html", "rb") as _f:
        _data = _f.read()
    resp = make_response(_data)
    resp.content_type = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Version"] = "1782288805"
    return resp

@pages_bp.route("/profile")
@pages_bp.route("/profile.html")
def profile_page():
    resp = send_from_directory(".", "profile.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp

@pages_bp.route("/api-docs")
@pages_bp.route("/api-docs.html")
def api_docs_page():
    return send_from_directory(".", "api-docs.html")

@pages_bp.route("/site-settings.js")
def site_settings_js():
    return send_from_directory(".", "site-settings.js")

@pages_bp.route("/favicon.ico")
def favicon():
    return "", 204
