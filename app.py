#!/usr/bin/env python3
"""
网盘资源浏览 Web 应用 v3.0
Flask + MySQL，支持连接池、admin认证、搜索限流
"""
import os
import time
import hashlib
import secrets
from functools import wraps
from datetime import datetime

import pymysql
import pymysql.cursors
import requests as http_requests
from flask import Flask, jsonify, request, send_from_directory, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ==================== 配置 ====================
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "172.23.0.2"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", "9zPwwKV_YYK3IcivNFcXuA"),
    "database": os.environ.get("DB_NAME", "pan_resource"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "connect_timeout": 5,
    "read_timeout": 10,
    "write_timeout": 10,
}

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin2026")

# ==================== 连接池 ====================
from dbutils.pooled_db import PooledDB

_pool = PooledDB(
    creator=pymysql,
    maxconnections=20,
    mincached=2,
    maxcached=5,
    blocking=True,
    maxusage=None,
    setsession=["SET NAMES utf8mb4"],
    ping=1,  # ping before use
    **DB_CONFIG,
)


def get_db():
    return _pool.connection()


# ==================== 限流 ====================
_rate_limits = {}  # key -> [count, window_start]
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 10  # max requests per window


def check_rate_limit(key):
    """Simple in-memory rate limiter. Returns True if allowed."""
    now = time.time()
    if key not in _rate_limits:
        _rate_limits[key] = [1, now]
        return True
    count, start = _rate_limits[key]
    if now - start > RATE_LIMIT_WINDOW:
        _rate_limits[key] = [1, now]
        return True
    if count >= RATE_LIMIT_MAX:
        return False
    _rate_limits[key][0] += 1
    return True


# ==================== Admin 认证 ====================
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "未登录，请先登录后台"}), 401
            return redirect("/login")
        return f(*args, **kwargs)

    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        data = request.get_json() or request.form
        username = data.get("username", "").strip()
        password = data.get("password", "")
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
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        if user["status"] != 1:
            return jsonify({"ok": False, "error": "账号已被禁用"}), 401
        # 验证密码：优先SHA256哈希，兼容空密码(首次登录用环境变量)
        pw_hash = hashlib.sha256(password.encode()).hexdigest()
        stored_pw = user.get("password", "")
        if stored_pw and stored_pw == pw_hash:
            pass  # 密码匹配
        elif not stored_pw and password == ADMIN_PASS:
            pass  # 空密码且环境变量匹配（首次登录兼容）
        else:
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        session["is_admin"] = user["role"] == "admin"
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
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
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>登录</title>
<style>
body{background:#0f0f14;color:#e4e4e7;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#1a1a24;border:1px solid #2a2a3e;border-radius:14px;padding:2rem;width:320px}
h2{text-align:center;margin-bottom:1.5rem}
input{width:100%;padding:.7rem;margin-bottom:1rem;border:1px solid #2a2a3e;border-radius:8px;background:#12121c;color:#e4e4e7;font-size:.9rem;box-sizing:border-box}
button{width:100%;padding:.7rem;border:none;border-radius:8px;background:#6366f1;color:white;font-size:.9rem;cursor:pointer}
button:hover{background:#4f46e5}
.err{color:#ef4444;font-size:.85rem;text-align:center;margin-bottom:.5rem;display:none}
</style></head><body>
<div class="box">
<h2>🔐 后台登录</h2>
<div class="err" id="err"></div>
<input id="u" placeholder="用户名" autofocus>
<input id="p" type="password" placeholder="密码" onkeydown="if(event.key==='Enter')doLogin()">
<button onclick="doLogin()">登录</button><a href="/" style="display:block;text-align:center;margin-top:0.8rem;color:#8888a8;font-size:0.85rem;text-decoration:none;transition:color 0.2s">← 返回首页</a>
</div>
<script>
async function doLogin(){
const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('p').value})});
if(r.ok){const d=await r.json();location.href=d.role==='admin'?'/admin':'/'}else{const d=await r.json();document.getElementById('err').style.display='block';document.getElementById('err').textContent=d.error||'登录失败'}
}
</script></body></html>"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ==================== 主页 ====================
@app.route("/")
def index():
    resp = send_from_directory(".", "index.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp


# ==================== API: 统计 ====================
@app.route("/api/stats")
def api_stats():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) as total FROM resources")
        total = cur.fetchone()["total"]
        cur.execute("SELECT DISTINCT type FROM resources WHERE type != '' ORDER BY type")
        types = [r["type"] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT quality FROM resources WHERE quality != '' ORDER BY quality")
        qualities = [r["quality"] for r in cur.fetchall()]
        return jsonify({"total": total, "types": types, "qualities": qualities})
    finally:
        db.close()


# ==================== API: 资源列表 ====================
@app.route("/api/resources")
def api_resources():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)
    query = request.args.get("q", "").strip()
    filter_type = request.args.get("filter", "all")
    source_filter = request.args.get("source", "all")

    offset = (page - 1) * per_page
    db = get_db()
    try:
        cur = db.cursor()
        conditions, params = [], []

        if query:
            conditions.append("(title LIKE %s OR note LIKE %s)")
            params.extend([f"%{query}%", f"%{query}%"])

        if filter_type != "all":
            if filter_type in ["电影", "剧集", "动漫", "综艺"]:
                conditions.append("type = %s")
                params.append(filter_type)
            elif filter_type in ["4K", "1080P", "720P"]:
                conditions.append("quality = %s")
                params.append(filter_type)

        if source_filter != "all":
            conditions.append("source = %s")
            params.append(source_filter)

        # 过滤词：排除含过滤词的资源
        f_sql, f_params = _filter_words_condition(cur)
        if f_sql:
            conditions.append(f_sql)
            params.extend(f_params)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cur.execute(f"SELECT COUNT(*) as total FROM resources {where}", params)
        total = cur.fetchone()["total"]
        total_pages = max(1, (total + per_page - 1) // per_page)

        cur.execute(
            f"""SELECT id, keyword, source, url, title, note, password,
                   quality, type, year, rating, datetime, created_at
            FROM resources {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s""",
            params + [per_page, offset],
        )
        items = cur.fetchall()

        return jsonify({
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        })
    finally:
        db.close()


# ==================== API: 来源统计 ====================
def _merge_source(src):
    """把 plugin:xxx 归类到主网盘类型"""
    if src.startswith("plugin:"):
        return "other"
    return src

def _filter_words_condition(cur):
    """返回 (sql_fragment, params) 用于排除含过滤词的资源"""
    cur.execute("SELECT word FROM filter_words")
    fwords = [r["word"] for r in cur.fetchall()]
    if not fwords:
        return "", []
    conds = []
    params = []
    for w in fwords:
        conds.append("(title NOT LIKE %s AND note NOT LIKE %s AND keyword NOT LIKE %s)")
        params.extend([f"%{w}%", f"%{w}%", f"%{w}%"])
    return "(" + " AND ".join(conds) + ")", params


@app.route("/api/sources")
def api_sources():
    """获取各来源统计，支持 ?q= 搜索词过滤，插件自动归类"""
    query = request.args.get("q", "").strip()
    db = get_db()
    try:
        cur = db.cursor()
        f_sql, f_params = _filter_words_condition(cur)
        f_where = "AND " + f_sql if f_sql else ""

        if query:
            cur.execute(
                "SELECT source, COUNT(*) as count FROM resources "
                "WHERE (title LIKE %s OR note LIKE %s) " + f_where + " "
                "GROUP BY source ORDER BY count DESC",
                [f"%{query}%", f"%{query}%"] + f_params,
            )
        else:
            cur.execute(
                "SELECT source, COUNT(*) as count FROM resources "
                "WHERE 1=1 " + f_where + " "
                "GROUP BY source ORDER BY count DESC",
                f_params,
            )
        rows = cur.fetchall()

        # 合并 plugin:xxx 到主类型
        merged = {}
        for r in rows:
            key = _merge_source(r["source"])
            merged[key] = merged.get(key, 0) + r["count"]

        result = [{"source": k, "count": v} for k, v in sorted(merged.items(), key=lambda x: -x[1])]
        return jsonify(result)
    finally:
        db.close()


# ==================== API: 关键词统计 ====================
@app.route("/api/keywords")
def api_keywords():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT keyword, COUNT(*) as count FROM resources GROUP BY keyword ORDER BY count DESC LIMIT 20"
        )
        return jsonify(list(cur.fetchall()))
    finally:
        db.close()


# ==================== API: 数据导入（带限流） ====================
@app.route("/api/import", methods=["POST"])
@admin_required
def api_import():
    client_ip = request.remote_addr
    if not check_rate_limit(f"import:{client_ip}"):
        return jsonify({"error": "请求太频繁，请1分钟后再试"}), 429

    data = request.get_json()
    keywords = data.get("keywords", [])
    search_limit = min(data.get("limit", 20), 50)

    if not keywords:
        return jsonify({"error": "请提供keywords参数"}), 400

    API_URL = "https://ps.aiqinghaiwork.cn/api/search"
    results = {"total_imported": 0, "details": [], "errors": []}

    db = get_db()
    try:
        with db.cursor() as cur:
            for kw in keywords:
                try:
                    resp = http_requests.post(
                        API_URL, json={"kw": kw, "limit": search_limit}, timeout=60
                    )
                    api_data = resp.json()
                    if api_data.get("code") != 0:
                        results["errors"].append(
                            {"keyword": kw, "error": api_data.get("message", "API错误")}
                        )
                        continue

                    total = api_data["data"]["total"]
                    merged = api_data["data"].get("merged_by_type", {})
                    imported = 0
                    for source, items in merged.items():
                        for item in items:
                            cur.execute(
                                """INSERT IGNORE INTO resources
                                (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                                (
                                    kw,
                                    source,
                                    item.get("url", ""),
                                    item.get("note", "") or item.get("title", ""),
                                    item.get("note", ""),
                                    item.get("password", ""),
                                    item.get("datetime", ""),
                                    "",
                                    "",
                                    0,
                                ),
                            )
                            imported += 1

                    db.commit()
                    results["total_imported"] += imported
                    results["details"].append(
                        {"keyword": kw, "api_total": total, "imported": imported}
                    )
                    time.sleep(1)

                except Exception as e:
                    results["errors"].append({"keyword": kw, "error": str(e)})
                    continue

            cur.execute("SELECT COUNT(*) as total FROM resources")
            results["db_total"] = cur.fetchone()["total"]

    finally:
        db.close()

    return jsonify(results)


# ==================== API: 健康检查 ====================
@app.route("/api/health")
def api_health():
    health = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {},
    }

    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) as total FROM resources")
        total = cur.fetchone()["total"]
        db.close()
        health["services"]["database"] = {"status": "connected", "resource_count": total}
    except Exception as e:
        health["services"]["database"] = {"status": "error", "error": str(e)}
        health["status"] = "degraded"

    return jsonify(health)


# ==================== 管理页面 ====================
@app.route("/admin")
@admin_required
def admin_page():
    resp = send_from_directory(".", "admin.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp


# ==================== 用户管理 API ====================
@app.route("/api/users")
@admin_required
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


@app.route("/api/users/<int:uid>", methods=["POST"])
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
            import hashlib
            pw_hash = hashlib.sha256(data["password"].encode()).hexdigest()
            sets.append("password=%s"); vals.append(pw_hash)
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


@app.route("/api/users/<int:uid>", methods=["DELETE"])
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

@app.route("/api/users", methods=["POST"])
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


# ==================== 用户信息 API ====================
@app.route("/api/me")
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

# ==================== 修改密码（任意登录用户） ====================
@app.route("/api/change_password", methods=["POST"])
def api_change_password():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json()
    new_pw = data.get("new_password", "").strip()
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error": "密码至少6位"}), 400
    pw_hash = hashlib.sha256(new_pw.encode()).hexdigest()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE users SET password=%s, updated_at=NOW() WHERE id=%s", (pw_hash, uid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()

# ==================== 收藏 API ====================
@app.route("/api/favorites")
def api_favorites():
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT f.id, f.resource_id, f.created_at, r.title, r.source, r.url, r.password, r.type, r.quality FROM favorites f LEFT JOIN resources r ON f.resource_id = r.id WHERE f.user_id = %s ORDER BY f.created_at DESC", (uid,))
        items = cur.fetchall()
        for i in items: i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
        return jsonify({"items": items, "total": len(items)})
    finally: db.close()

@app.route("/api/favorites", methods=["POST"])
def api_fav_add():
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    data = request.get_json()
    rid = data.get("resource_id")
    if not rid: return jsonify({"error": "缺少resource_id"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("INSERT IGNORE INTO favorites (user_id, resource_id) VALUES (%s, %s)", (uid, rid))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()

@app.route("/api/favorites/<int:rid>", methods=["DELETE"])
def api_fav_del(rid):
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM favorites WHERE user_id = %s AND resource_id = %s", (uid, rid))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()

# ==================== 搜索历史 API ====================
@app.route("/api/search_history")
def api_search_hist():
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT keyword, COUNT(*) as cnt, MAX(created_at) as last_at FROM search_history WHERE user_id = %s GROUP BY keyword ORDER BY last_at DESC LIMIT 30", (uid,))
        items = cur.fetchall()
        for i in items: i["last_at"] = str(i["last_at"]) if i["last_at"] else ""
        return jsonify({"items": items})
    finally: db.close()

@app.route("/api/search_history", methods=["POST"])
def api_search_hist_add():
    uid = session.get("user_id")
    if not uid: return jsonify({"ok": False}), 200
    data = request.get_json()
    kw = data.get("keyword", "").strip()
    if not kw: return jsonify({"ok": False}), 200
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("INSERT INTO search_history (user_id, keyword) VALUES (%s, %s)", (uid, kw))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()

@app.route("/api/search_history", methods=["DELETE"])
def api_search_hist_clear():
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM search_history WHERE user_id = %s", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()

# ==================== 点击统计 ====================
@app.route("/api/click/<int:rid>", methods=["POST"])
def api_click(rid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE resources SET click_count = click_count + 1 WHERE id = %s", (rid,))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()
# ==================== 个人中心页面 ====================
@app.route("/profile")
@app.route("/profile.html")
def profile_page():
    resp = send_from_directory(".", "profile.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp


# ==================== 搜索日志收集（所有搜索） ====================
@app.route("/api/search_log", methods=["POST"])
def api_search_log():
    """记录搜索关键词（含未登录用户）"""
    data = request.get_json()
    kw = data.get("keyword", "").strip()
    if not kw or len(kw) < 2:
        return jsonify({"ok": False}), 200
    uid = session.get("user_id")
    ip = request.remote_addr
    db = get_db()
    try:
        cur = db.cursor()
        # 检查过滤词
        cur.execute("SELECT word FROM filter_words")
        blocked = [r["word"].lower() for r in cur.fetchall()]
        if any(b in kw.lower() for b in blocked):
            _log_op(uid, session.get("username"), "search_blocked", f"关键词: {kw}", ip)
            return jsonify({"ok": False, "blocked": True}), 200
        # 记录搜索
        cur.execute("INSERT INTO search_logs (user_id, keyword, ip) VALUES (%s, %s, %s)", (uid, kw, ip))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


def _log_op(uid, username, action, detail, ip=None):
    """记录操作日志的内部函数"""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO operation_logs (user_id, username, action, detail, ip) VALUES (%s, %s, %s, %s, %s)",
                    (uid, username, action, detail, ip or request.remote_addr))
        db.commit()
        db.close()
    except: pass


# ==================== 管理员：待导入搜索词 ====================
@app.route("/api/admin/pending_imports")
@admin_required
def api_pending_imports():
    """获取待导入的搜索关键词（聚合去重）"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("""
            SELECT keyword, COUNT(*) as search_count, MAX(created_at) as last_search
            FROM search_logs WHERE is_imported = 0
            GROUP BY keyword ORDER BY search_count DESC, last_search DESC
            LIMIT 100
        """)
        items = cur.fetchall()
        for i in items:
            i["last_search"] = str(i["last_search"]) if i["last_search"] else ""
        return jsonify({"items": items, "total": len(items)})
    finally:
        db.close()


# ==================== 管理员：确认导入 ====================
@app.route("/api/admin/confirm_import", methods=["POST"])
@admin_required
def api_confirm_import():
    """管理员确认导入指定关键词"""
    data = request.get_json()
    keywords = data.get("keywords", [])
    if not keywords:
        return jsonify({"error": "请选择关键词"}), 400

    import requests as http_requests
    API_URL = "https://ps.aiqinghaiwork.cn/api/search"
    results = {"total_imported": 0, "details": []}

    db = get_db()
    try:
        cur = db.cursor()
        for kw in keywords:
            try:
                resp = http_requests.post(API_URL, json={"kw": kw, "limit": 30}, timeout=60)
                api_data = resp.json()
                if api_data.get("code") != 0:
                    results["details"].append({"keyword": kw, "error": "API错误"})
                    continue
                merged = api_data["data"].get("merged_by_type", {})
                imported = 0
                for source, items in merged.items():
                    for item in items:
                        cur.execute("""INSERT IGNORE INTO resources
                            (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                            (kw, source, item.get("url", ""), item.get("note", "") or item.get("title", ""),
                             item.get("note", ""), item.get("password", ""), item.get("datetime", ""), "", "", 0))
                        imported += 1
                # 标记为已导入
                cur.execute("UPDATE search_logs SET is_imported = 1 WHERE keyword = %s", (kw,))
                db.commit()
                results["total_imported"] += imported
                results["details"].append({"keyword": kw, "imported": imported})
                time.sleep(1)
            except Exception as e:
                results["details"].append({"keyword": kw, "error": str(e)})
        _log_op(session.get("user_id"), session.get("username"), "import", f"导入{len(keywords)}个关键词")
    finally:
        db.close()
    return jsonify(results)


# ==================== 管理员：操作日志 ====================
@app.route("/api/admin/logs")
@admin_required
def api_operation_logs():
    """获取操作日志"""
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 200)
    action = request.args.get("action", "")
    offset = (page - 1) * per_page
    db = get_db()
    try:
        cur = db.cursor()
        conditions, params = [], []
        if action:
            conditions.append("action = %s")
            params.append(action)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(f"SELECT COUNT(*) as total FROM operation_logs {where}", params)
        total = cur.fetchone()["total"]
        cur.execute(f"SELECT * FROM operation_logs {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    params + [per_page, offset])
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
        return jsonify({"items": items, "total": total, "page": page})
    finally:
        db.close()


# ==================== 过滤词管理 ====================
@app.route("/api/admin/filter_words")
@admin_required
def api_filter_words():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM filter_words ORDER BY category, id")
        items = cur.fetchall()
        for i in items: i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
        return jsonify({"items": items, "total": len(items)})
    finally:
        db.close()

@app.route("/api/admin/filter_words", methods=["POST"])
@admin_required
def api_filter_words_add():
    data = request.get_json()
    word = data.get("word", "").strip()
    cat = data.get("category", "general")
    if not word: return jsonify({"error": "请输入过滤词"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("INSERT IGNORE INTO filter_words (word, category) VALUES (%s, %s)", (word, cat))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/admin/filter_words/<int:fid>", methods=["DELETE"])
@admin_required
def api_filter_words_del(fid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM filter_words WHERE id = %s", (fid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ==================== Favicon ====================

@app.route("/favicon.ico")
def favicon():
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
