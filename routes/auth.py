"""Authentication and user management."""
import pymysql
from flask import Blueprint, jsonify, redirect, request, send_from_directory, session

from models.database import get_db
from utils.auth import (
    admin_required,
    check_login_limit,
    hash_password,
    record_login_failure,
    reset_login_failures,
    verify_password,
)

auth_bp = Blueprint("auth", __name__)
@auth_bp.route("/api/user/me")
def api_user_me():
    """返回当前登录用户信息"""
    if session.get("user_id"):
        return jsonify({
            "logged_in": True,
            "username": session.get("username", ""),
            "is_admin": session.get("is_admin", False),
        })
    return jsonify({"logged_in": False})

def api_users():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    offset = (page - 1) * per_page

    db = get_db()
    try:
        cur = db.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT COUNT(*) as total FROM users")
        total = cur.fetchone()["total"]

        cur.execute(
            """SELECT id, username, email, role, status, last_login, created_at, updated_at
            FROM users ORDER BY id ASC LIMIT %s OFFSET %s""",
            (per_page, offset),
        )
        items = cur.fetchall()

        for i in items:
            i["last_login"] = str(i["last_login"]) if i["last_login"] else ""
            i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
            i["updated_at"] = str(i["updated_at"]) if i["updated_at"] else ""

        return jsonify({"items": items, "total": total, "page": page})
    finally:
        db.close()

@auth_bp.route("/api/users/<int:uid>", methods=["POST"])
@admin_required
def api_user_update(uid):
    """更新用户信息：username, email, password, role, status"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "无数据"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        sets, vals = [], []
        if "username" in data:
            sets.append("username=%s"); vals.append(data["username"])
        if "email" in data:
            sets.append("email=%s"); vals.append(data["email"])
        if "password" in data and data["password"]:
            sets.append("password=%s"); vals.append(hash_password(data["password"]))
        if "role" in data:
            sets.append("role=%s"); vals.append(data["role"])
        if "status" in data:
            sets.append("status=%s"); vals.append(data["status"])
        if not sets:
            return jsonify({"error": "无有效字段"}), 400
        sets.append("updated_at=NOW()")
        vals.append(uid)
        cur.execute(f"UPDATE users SET {','.join(sets)} WHERE id=%s", vals)
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    except pymysql.err.IntegrityError:
        return jsonify({"error": "用户名已存在"}), 409
    finally:
        db.close()

@auth_bp.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_user_delete(uid):
    """删除用户：不能删自己，不能删其他管理员"""
    my_id = session.get("user_id")
    if uid == my_id:
        return jsonify({"error": "不能删除自己"}), 403
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT role FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()
        if not u:
            return jsonify({"error": "用户不存在"}), 404
        if u["role"] == "admin":
            return jsonify({"error": "不能删除其他管理员"}), 403
        cur.execute("DELETE FROM users WHERE id=%s", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()

@auth_bp.route("/api/users", methods=["POST"])
@admin_required
def api_user_add():
    data = request.get_json()
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"error": "用户名不能为空"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO users (username, email, role, status) VALUES (%s, %s, %s, %s)",
            (username, data.get("email", ""), data.get("role", "user"), 1),
        )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    except pymysql.err.IntegrityError:
        return jsonify({"error": f'用户名 "{username}" 已存在'}), 409
    finally:
        db.close()

@auth_bp.route("/api/me")
def api_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"logged_in": False})
    # 从数据库读取最新角色（避免session过期问题）
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT username, role FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()
    finally:
        db.close()
    if not u:
        return jsonify({"logged_in": False})
    is_admin = u["role"] == "admin"
    session["is_admin"] = is_admin
    session["username"] = u["username"]
    return jsonify({"logged_in": True, "user_id": uid, "username": u["username"], "is_admin": is_admin})

@auth_bp.route("/api/change_password", methods=["POST"])
def api_change_password():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json()
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "").strip()
    if not old_pw:
        return jsonify({"error": "请输入旧密码"}), 400
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error": "密码至少6位"}), 400
    # 验证旧密码（兼容旧版 SHA256）
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT password FROM users WHERE id=%s", (uid,))
        user = cur.fetchone()
        if not user:
            return jsonify({"error": "用户不存在"}), 404
        if not verify_password(old_pw, user.get("password", "")):
            return jsonify({"error": "旧密码错误"}), 403
        new_hash = hash_password(new_pw)
        cur.execute("UPDATE users SET password=%s, updated_at=NOW() WHERE id=%s", (new_hash, uid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()
