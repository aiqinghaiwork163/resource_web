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
app.secret_key = os.environ.get("SECRET_KEY", "resource_web_2026_stable_key_xK9mP2vQ")

import gzip
import io
from functools import wraps

# ==================== Gzip 压缩 ====================
@app.after_request
def add_gzip(response):
    """对 JSON/HTML/CSS/JS 响应启用 gzip 压缩"""
    try:
        if response.status_code != 200:
            return response
        accept = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accept:
            return response
        ct = response.content_type or ""
        if not any(t in ct for t in ["json", "html", "javascript", "css", "text"]):
            return response
        data = response.get_data()
        if len(data) < 500:
            return response
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as f:
            f.write(data)
        response.set_data(buf.getvalue())
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = len(response.get_data())
        response.headers["Vary"] = "Accept-Encoding"
    except (RuntimeError, TypeError):
        pass  # send_from_directory 直接流式响应，跳过压缩
    return response


# ==================== 图片代理缓存 ====================
import hashlib
_img_cache = {}  # url -> (data, content_type, expire)

@app.route("/api/img_proxy")
def img_proxy():
    """代理 TMDB 图片，本地内存缓存 24 小时"""
    url = request.args.get("url", "")
    if not url or "tmdb.org" not in url:
        return "", 400
    now = time.time()
    if url in _img_cache:
        data, ct, expire = _img_cache[url]
        if now < expire:
            return data, 200, {"Content-Type": ct, "Cache-Control": "public, max-age=86400"}
    try:
        import requests as _req
        r = _req.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            ct = r.headers.get("Content-Type", "image/jpeg")
            _img_cache[url] = (r.content, ct, now + 86400)
            return r.content, 200, {"Content-Type": ct, "Cache-Control": "public, max-age=86400"}
    except Exception:
        pass
    return "", 404


# ==================== 静态文件长缓存 ====================
@app.after_request
def cache_static(response):
    """静态文件设置长缓存"""
    if request.path.startswith("/api/") or request.path.startswith("/login"):
        return response
    if response.status_code == 200 and not response.headers.get("Cache-Control"):
        response.headers["Cache-Control"] = "public, max-age=300"
    return response



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
    conn = _pool.connection()
    try:
        with conn.cursor() as _cur:
            _cur.execute("SET time_zone = '+08:00'")
    except Exception:
        pass
    return conn


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


# ==================== 内存缓存 ====================
_cache = {}  # key -> (data, expire_time)


def cached(ttl=300):
    """简单内存缓存装饰器，ttl秒内返回缓存数据"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = request.url
            now = time.time()
            if key in _cache:
                data, expire = _cache[key]
                if now < expire:
                    return data
            result = f(*args, **kwargs)
            _cache[key] = (result, now + ttl)
            return result
        wrapper.__name__ = f.__name__
        wrapper.__doc__ = f.__doc__
        return wrapper
    return decorator



@app.route("/api/debug/session")
def debug_session():
    """Debug: check session state"""
    return jsonify({
        "is_admin": session.get("is_admin"),
        "user_id": session.get("user_id"),
        "username": session.get("username"),
        "session_keys": list(session.keys()),
    })

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
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 - 网盘资源浏览器</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:linear-gradient(135deg,#032541 0%,#0d253f 50%,#01b4e4 150%);color:#fff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}
.login-card{background:rgba(10,22,40,0.95);border:1px solid rgba(1,180,228,0.15);border-radius:16px;padding:2.5rem;width:100%;max-width:380px;backdrop-filter:blur(20px);box-shadow:0 20px 60px rgba(0,0,0,0.5)}
.logo-area{text-align:center;margin-bottom:2rem}
.logo-area h1{font-size:1.5rem;font-weight:700;margin-bottom:0.3rem}
.logo-area h1 i{color:#01b4e4;margin-right:0.5rem}
.logo-area p{color:#999;font-size:0.85rem}
.input-group{position:relative;margin-bottom:1.2rem}
.input-group i{position:absolute;left:1rem;top:50%;transform:translateY(-50%);color:#999;font-size:0.9rem}
.input-group input{width:100%;padding:0.85rem 1rem 0.85rem 2.8rem;border:1px solid #1a3a5c;border-radius:10px;background:#0a1628;color:#fff;font-size:0.95rem;outline:none;transition:border-color 0.2s}
.input-group input:focus{border-color:#01b4e4}
.input-group input::placeholder{color:#557}
.btn-login{width:100%;padding:0.85rem;border:none;border-radius:10px;background:linear-gradient(135deg,#01b4e4,#0189a8);color:white;font-size:1rem;font-weight:600;cursor:pointer;transition:all 0.3s;margin-top:0.5rem}
.btn-login:hover{transform:translateY(-2px);box-shadow:0 8px 25px rgba(1,180,228,0.3)}
.btn-login:active{transform:translateY(0)}
.error-msg{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);color:#ef4444;padding:0.6rem 1rem;border-radius:8px;font-size:0.85rem;text-align:center;margin-bottom:1rem;display:none}
.back-link{display:block;text-align:center;margin-top:1.5rem;color:#999;font-size:0.85rem;text-decoration:none;transition:color 0.2s}
.back-link:hover{color:#01b4e4}
</style></head><body>
<div class="login-card">
  <div class="logo-area">
    <h1><i class="fas fa-cloud"></i>网盘资源浏览器</h1>
    <p>登录后台管理</p>
  </div>
  <div class="error-msg" id="err"></div>
  <div class="input-group">
    <i class="fas fa-user"></i>
    <input id="u" placeholder="用户名" autofocus>
  </div>
  <div class="input-group">
    <i class="fas fa-lock"></i>
    <input id="p" type="password" placeholder="密码" onkeydown="if(event.key==='Enter')doLogin()">
  </div>
  <button class="btn-login" onclick="doLogin()"><i class="fas fa-sign-in-alt"></i> 登录</button>
  <a href="/" class="back-link"><i class="fas fa-arrow-left"></i> 返回首页</a>
</div>
<script>
async function doLogin(){
  var err=document.getElementById('err');err.style.display='none';
  var r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('p').value})});
  if(r.ok){var d=await r.json();location.href=d.role==='admin'?'/admin':'/'}
  else{var d=await r.json();err.style.display='block';err.textContent=d.error||'登录失败';}
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
            "importing": _auto_import_trigger(query) if total == 0 and query else False,
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
    """获取待导入的搜索关键词（聚合去重，排除已有资源的关键词）"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("""
            SELECT sl.keyword, COUNT(*) as search_count, MAX(created_at) as last_search
            FROM search_logs sl
            WHERE sl.is_imported = 0
              AND NOT EXISTS (
                  SELECT 1 FROM resources r WHERE r.keyword = sl.keyword
              )
            GROUP BY sl.keyword ORDER BY search_count DESC, last_search DESC
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



# ==================== 新功能 API ====================

# ── ① 热门搜索排行 ──
@app.route("/api/hot_searches")
def api_hot_searches():
    """返回 Top 10 热搜词"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT keyword, hit_count as count FROM search_suggestions "
            "ORDER BY hit_count DESC LIMIT 10"
        )
        return jsonify({"items": cur.fetchall()})
    finally:
        db.close()


# ── ② 搜索自动补全 ──
@app.route("/api/autocomplete")
def api_autocomplete():
    """输入时返回匹配的搜索建议"""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 1:
        return jsonify({"items": []})
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT keyword, hit_count FROM search_suggestions "
            "WHERE keyword LIKE %s ORDER BY hit_count DESC LIMIT 8",
            (f"%{q}%",)
        )
        return jsonify({"items": cur.fetchall()})
    finally:
        db.close()


# ── ③ 资源详情面板 ──
@app.route("/api/resource/<int:rid>/detail")
def api_resource_detail(rid):
    """返回资源完整详情（含TMDB数据）"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, keyword, source, url, title, note, password, "
            "quality, type, year, rating, datetime, images, "
            "tmdb_id, created_at "
            "FROM resources WHERE id = %s", (rid,)
        )
        item = cur.fetchone()
        if not item:
            return jsonify({"error": "资源不存在"}), 404
        return jsonify({"item": item})
    finally:
        db.close()


# ── ④ TMDB 热门排行 ──
@app.route("/api/tmdb/trending")
def api_tmdb_trending():
    """TMDB 热门排行（绕GFW直连）"""
    import socket, ssl as _ssl, json as _json, urllib.parse
    media = request.args.get("media", "all")  # all/movie/tv
    time_window = request.args.get("time", "week")  # day/week
    page = min(int(request.args.get("page", 1)), 10)
    API_KEY = os.environ.get("TMDB_API_KEY", "91f75e1241354f2503a397fc7a276408")

    ep = f"trending/{media}/{time_window}"
    path = f"/3/{ep}?api_key={API_KEY}&language=zh-CN&page={page}"
    try:
        ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
        s = socket.create_connection((str(ip), 443), 10)
        ss = _ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
        ss.sendall(
            f"GET {path} HTTP/1.1\r\nHost: api.themoviedb.org\r\n"
            f"Accept: application/json\r\nConnection: close\r\n\r\n".encode()
        )
        r = b""
        while True:
            c = ss.recv(65536)
            if not c:
                break
            r += c
        ss.close()
        h = r.find(b"\r\n\r\n")
        if h < 0:
            return jsonify({"items": [], "error": "no response"})
        body = r[h + 4:]
        if b"chunked" in r[:h]:
            r2 = body; body = b""
            while r2:
                e = r2.find(b"\r\n")
                if e < 0:
                    break
                try:
                    sz = int(r2[:e], 16)
                except ValueError:
                    break
                if sz == 0:
                    break
                body += r2[e + 2:e + 2 + sz]
                r2 = r2[e + 2 + sz + 2:]
        data = _json.loads(body)
        items = []
        for it in data.get("results", [])[:20]:
            items.append({
                "title": it.get("title") or it.get("name") or "",
                "overview": (it.get("overview") or "")[:200],
                "poster": ("/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w500" + it["poster_path"])) if it.get("poster_path") else "",
                "backdrop": ("/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w780" + it["backdrop_path"])) if it.get("backdrop_path") else "",
                "rating": round(it.get("vote_average", 0), 1),
                "media_type": it.get("media_type", ""),
                "year": (it.get("release_date") or it.get("first_air_date") or "")[:4],
                "tmdb_id": it.get("id"),
            })
        return jsonify({"items": items, "total": data.get("total_results", 0)})
    except Exception as e:
        return jsonify({"items": [], "error": str(e)})


# ── ④ 推荐系统 ──
@app.route("/api/recommendations")
def api_recommendations():
    """TMDB 热门排行 + 本地有图资源混合推荐"""
    limit = min(int(request.args.get("limit", 12)), 30)
    db = get_db()
    try:
        cur = db.cursor()
        # 1. 先取有图片的本地热门资源
        cur.execute(
            "SELECT id, title, note, source, url, quality, type, year, rating, images "
            "FROM resources WHERE images IS NOT NULL AND images != '' "
            "ORDER BY created_at DESC LIMIT %s", (limit,)
        )
        local_items = cur.fetchall()
        for it in local_items:
            it["source_type"] = "local"
            # 解析 images JSON 获取海报
            imgs = it.get("images", "")
            if imgs:
                try:
                    import json as _j
                    arr = _j.loads(imgs)
                    it["poster"] = arr[0] if arr else ""
                except Exception:
                    it["poster"] = imgs
            else:
                it["poster"] = ""
        return jsonify({"items": local_items, "strategy": "local_with_posters"})
    finally:
        db.close()


# ── ⑤ 关键词订阅 ──
@app.route("/api/subscriptions")
def api_subscriptions():
    """获取当前用户的关键词订阅"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"items": []})
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, keyword, created_at FROM subscriptions "
            "WHERE user_id = %s ORDER BY created_at DESC", (uid,)
        )
        return jsonify({"items": cur.fetchall()})
    finally:
        db.close()


@app.route("/api/subscriptions", methods=["POST"])
def api_subscription_add():
    """添加关键词订阅"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "请先登录"}), 401
    data = request.get_json() or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "请输入关键词"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT IGNORE INTO subscriptions (user_id, keyword) VALUES (%s, %s)",
            (uid, keyword)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/subscriptions/<int:sid>", methods=["DELETE"])
def api_subscription_del(sid):
    """取消关键词订阅"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "请先登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM subscriptions WHERE id = %s AND user_id = %s", (sid, uid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ── ⑧ 资源分享 ──
@app.route("/api/resource/<int:rid>/share", methods=["POST"])
def api_resource_share(rid):
    """生成分享链接"""
    import secrets
    token = secrets.token_urlsafe(16)
    uid = session.get("user_id")
    db = get_db()
    try:
        cur = db.cursor()
        # 检查资源是否存在
        cur.execute("SELECT id FROM resources WHERE id = %s", (rid,))
        if not cur.fetchone():
            return jsonify({"error": "资源不存在"}), 404
        cur.execute(
            "INSERT INTO share_links (resource_id, token, created_by) VALUES (%s, %s, %s)",
            (rid, token, uid)
        )
        db.commit()
        share_url = f"{request.host_url}s/{token}"
        return jsonify({"ok": True, "url": share_url, "token": token})
    finally:
        db.close()


@app.route("/s/<token>")
def api_shared_resource(token):
    """通过分享链接访问资源"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT sl.resource_id, sl.click_count, "
            "r.id, r.title, r.note, r.source, r.url, r.quality, r.type, "
            "r.year, r.rating, r.poster_url, r.password "
            "FROM share_links sl "
            "JOIN resources r ON r.id = sl.resource_id "
            "WHERE sl.token = %s", (token,)
        )
        item = cur.fetchone()
        if not item:
            return "链接无效或已过期", 404
        # 更新点击数
        cur.execute(
            "UPDATE share_links SET click_count = click_count + 1 WHERE token = %s", (token,)
        )
        db.commit()
        # 返回简洁的分享页面
        title = item.get("title") or item.get("note") or "网盘资源"
        url = item.get("url", "")
        source = item.get("source", "")
        quality = item.get("quality", "")
        password = item.get("password", "")
        poster = item.get("poster_url", "")

        poster_html = ""
        if poster:
            poster_html = '<img src="' + poster + '" style="max-width:200px;border-radius:8px;margin-bottom:1rem">'

        quality_html = ""
        if quality:
            quality_html = '<span class="tag">' + quality + '</span>'

        pw_html = ""
        if password:
            pw_html = '<div class="pw">🔑 提取码: ' + password + '</div>'

        html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + title + ' - 分享</title>'
            '<style>'
            '*{margin:0;padding:0;box-sizing:border-box}'
            'body{background:#0f0f14;color:#e4e4e7;font-family:-apple-system,sans-serif;'
            'display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}'
            '.card{background:#1a1a24;border:1px solid #2a2a3e;border-radius:16px;padding:2rem;max-width:480px;width:100%;text-align:center}'
            'h2{font-size:1.2rem;margin-bottom:0.5rem}'
            '.tag{display:inline-block;background:#6366f1;color:white;padding:0.2rem 0.6rem;border-radius:6px;font-size:0.75rem;margin:0.2rem}'
            '.url{background:#12121c;border:1px solid #2a2a3e;border-radius:8px;padding:0.8rem;word-break:break-all;margin:1rem 0;font-size:0.85rem;user-select:all}'
            '.pw{color:#f59e0b;font-weight:600;margin:0.5rem 0}'
            '.btn{display:inline-block;background:#6366f1;color:white;padding:0.7rem 2rem;border:none;border-radius:8px;font-size:0.9rem;cursor:pointer;text-decoration:none;margin:0.3rem}'
            '.btn:hover{background:#4f46e5}'
            '.btn2{background:#374151}'
            '</style></head><body><div class="card">'
            + poster_html
            + '<h2>' + title + '</h2>'
            '<div><span class="tag">' + source + '</span>' + quality_html + '</div>'
            '<div class="url" id="u">' + url + '</div>'
            + pw_html
            + '<button class="btn" onclick="copyU()">📋 复制链接</button>'
            '<a class="btn btn2" href="' + url + '" target="_blank">🔗 打开网盘</a>'
            '<a class="btn btn2" href="/" style="text-decoration:none">🏠 更多资源</a>'
            '<script>function copyU(){var u=document.getElementById("u").innerText;'
            'navigator.clipboard.writeText(u).then(function(){alert("已复制！")})}</script>'
            '</div></body></html>'
        )
        return html
    finally:
        db.close()


# ── ⑨ 后台数据统计面板 ──
@app.route("/api/admin/stats/dashboard")
@admin_required
def api_admin_dashboard():
    """后台统计数据（图表用）"""
    db = get_db()
    try:
        cur = db.cursor()

        # 1. 每日新增资源（最近30天）
        cur.execute(
            "SELECT DATE(created_at) as date, COUNT(*) as count "
            "FROM resources WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
            "GROUP BY DATE(created_at) ORDER BY date"
        )
        daily_resources = cur.fetchall()

        # 2. 每日搜索量（最近30天）
        cur.execute(
            "SELECT DATE(created_at) as date, COUNT(*) as count "
            "FROM search_logs WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
            "GROUP BY DATE(created_at) ORDER BY date"
        )
        daily_searches = cur.fetchall()

        # 3. Top 20 搜索词
        cur.execute(
            "SELECT keyword, COUNT(*) as count FROM search_logs "
            "GROUP BY keyword ORDER BY count DESC LIMIT 20"
        )
        top_searches = cur.fetchall()

        # 4. 资源类型分布
        cur.execute(
            "SELECT COALESCE(type, '未分类') as type, COUNT(*) as count "
            "FROM resources GROUP BY type ORDER BY count DESC"
        )
        type_dist = cur.fetchall()

        # 5. 来源分布
        cur.execute(
            "SELECT source, COUNT(*) as count FROM resources "
            "GROUP BY source ORDER BY count DESC LIMIT 10"
        )
        source_dist = cur.fetchall()

        # 6. 画质分布
        cur.execute(
            "SELECT COALESCE(quality, '未知') as quality, COUNT(*) as count "
            "FROM resources GROUP BY quality ORDER BY count DESC"
        )
        quality_dist = cur.fetchall()

        # 7. 总览数据
        stats = {}
        for tbl in ["resources", "users", "search_logs", "favorites", "filter_words"]:
            cur.execute(f"SELECT COUNT(*) as c FROM {tbl}")
            stats[tbl] = cur.fetchone()["c"]

        return jsonify({
            "daily_resources": daily_resources,
            "daily_searches": daily_searches,
            "top_searches": top_searches,
            "type_dist": type_dist,
            "source_dist": source_dist,
            "quality_dist": quality_dist,
            "overview": stats,
        })
    finally:
        db.close()


# ── ⑩ API 文档 ──
@app.route("/api/docs")
def api_docs():
    """自动生成 API 文档"""
    docs = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        func = app.view_functions.get(rule.endpoint)
        if not func:
            continue
        doc = {
            "path": rule.rule,
            "methods": sorted(rule.methods - {"OPTIONS", "HEAD"}),
            "name": func.__name__,
            "doc": (func.__doc__ or "").strip(),
            "auth": hasattr(func, "__wrapped__"),
        }
        docs.append(doc)
    docs.sort(key=lambda d: d["path"])
    return jsonify({"docs": docs, "total": len(docs)})



# ==================== 后台管理增强 API ====================

# ── 系统设置 ──
@app.route("/api/admin/settings")
@admin_required
def api_admin_settings():
    """获取所有系统设置"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT `key`, value, description, updated_at FROM settings ORDER BY `key`")
        items = cur.fetchall()
        for i in items:
            i["updated_at"] = str(i["updated_at"]) if i.get("updated_at") else ""
        return jsonify({"items": items})
    finally:
        db.close()


@app.route("/api/admin/settings", methods=["POST"])
@admin_required
def api_admin_settings_update():
    """更新系统设置"""
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        updated = 0
        for k, v in data.items():
            cur.execute(
                "UPDATE settings SET value=%s WHERE `key`=%s",
                (str(v), k)
            )
            updated += cur.rowcount
        db.commit()
        # 清除相关缓存
        for k in list(_cache.keys()):
            if "/api/" in k:
                del _cache[k]
        return jsonify({"ok": True, "updated": updated})
    finally:
        db.close()


# ── 资源批量操作 ──
@app.route("/api/admin/resources/batch", methods=["POST"])
@admin_required
def api_admin_resources_batch():
    """批量操作资源: delete/export/update_quality/update_type"""
    data = request.get_json() or {}
    action = data.get("action", "")
    ids = data.get("ids", [])
    keyword = data.get("keyword", "")
    
    if not ids and not keyword:
        return jsonify({"error": "请选择资源或指定关键词"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        
        if keyword:
            where = "WHERE keyword = %s"
            params = [keyword]
        else:
            placeholders = ",".join(["%s"] * len(ids))
            where = f"WHERE id IN ({placeholders})"
            params = ids

        if action == "delete":
            cur.execute(f"DELETE FROM resources {where}", params)
            db.commit()
            return jsonify({"ok": True, "affected": cur.rowcount})

        elif action == "export":
            cur.execute(
                f"SELECT id, title, note, source, url, quality, type, password, keyword "
                f"FROM resources {where} ORDER BY created_at DESC LIMIT 5000",
                params
            )
            items = cur.fetchall()
            lines = []
            for it in items:
                title = it.get("title") or it.get("note") or ""
                lines.append(f"{title}	{it.get('source','')}	{it.get('url','')}	{it.get('password','')}")
            return jsonify({"ok": True, "data": "\n".join(lines), "count": len(items)})

        elif action == "update_quality":
            quality = data.get("value", "")
            cur.execute(f"UPDATE resources SET quality = %s {where}", [quality] + params)
            db.commit()
            return jsonify({"ok": True, "affected": cur.rowcount})

        elif action == "update_type":
            rtype = data.get("value", "")
            cur.execute(f"UPDATE resources SET type = %s {where}", [rtype] + params)
            db.commit()
            return jsonify({"ok": True, "affected": cur.rowcount})

        return jsonify({"error": f"未知操作: {action}"}), 400
    finally:
        db.close()


# ── 资源列表（后台增强版） ──
@app.route("/api/admin/resources")
@admin_required
def api_admin_resources():
    """后台资源列表，支持搜索/排序/分页"""
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 50)), 200)
    q = request.args.get("q", "").strip()
    source = request.args.get("source", "")
    rtype = request.args.get("type", "")
    quality = request.args.get("quality", "")
    sort = request.args.get("sort", "created_at")
    order = request.args.get("order", "desc")

    offset = (page - 1) * per_page
    db = get_db()
    try:
        cur = db.cursor()
        conditions, params = [], []
        if q:
            conditions.append("(title LIKE %s OR note LIKE %s OR keyword LIKE %s)")
            params.extend([f"%{q}%"] * 3)
        if source:
            conditions.append("source = %s")
            params.append(source)
        if rtype:
            conditions.append("type = %s")
            params.append(rtype)
        if quality:
            conditions.append("quality = %s")
            params.append(quality)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        allowed_sorts = {"created_at", "id", "title", "source", "quality", "type", "click_count"}
        if sort not in allowed_sorts:
            sort = "created_at"
        order_dir = "DESC" if order == "desc" else "ASC"

        cur.execute(f"SELECT COUNT(*) as total FROM resources {where}", params)
        total = cur.fetchone()["total"]

        cur.execute(
            f"SELECT id, title, note, source, url, quality, type, year, keyword, "
            f"click_count, created_at FROM resources {where} "
            f"ORDER BY {sort} {order_dir} LIMIT %s OFFSET %s",
            params + [per_page, offset]
        )
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""

        # 统计信息
        cur.execute("SELECT COUNT(*) as c FROM resources")
        total_all = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(DISTINCT source) as c FROM resources")
        source_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(DISTINCT keyword) as c FROM resources")
        kw_count = cur.fetchone()["c"]

        return jsonify({
            "items": items, "total": total, "page": page, "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "stats": {"total_all": total_all, "source_count": source_count, "keyword_count": kw_count}
        })
    finally:
        db.close()


# ── 自动导入队列 ──
@app.route("/api/admin/import_queue")
@admin_required
def api_admin_import_queue():
    """获取自动导入队列"""
    status = request.args.get("status", "")
    db = get_db()
    try:
        cur = db.cursor()
        if status:
            cur.execute(
                "SELECT * FROM import_queue WHERE status = %s ORDER BY created_at DESC LIMIT 100",
                (status,)
            )
        else:
            cur.execute("SELECT * FROM import_queue ORDER BY created_at DESC LIMIT 100")
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""
            i["updated_at"] = str(i["updated_at"]) if i.get("updated_at") else ""
        return jsonify({"items": items, "total": len(items)})
    finally:
        db.close()


@app.route("/api/admin/import_queue/retry", methods=["POST"])
@admin_required
def api_admin_import_retry():
    """重试失败的导入任务"""
    data = request.get_json() or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "请选择任务"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(
            f"UPDATE import_queue SET status='pending', error_msg=NULL WHERE id IN ({placeholders}) AND status='failed'",
            ids
        )
        db.commit()
        return jsonify({"ok": True, "reset": cur.rowcount})
    finally:
        db.close()


# ── 自动导入逻辑（搜索0结果时自动触发并处理） ──
def _auto_import_trigger(keyword):
    """当搜索无结果时，自动加入队列并立即后台处理。返回值: True=导入中, 'exhausted'=已尝试过无结果, False=不触发"""
    try:
        db = get_db()
        cur = db.cursor()
        # 检查是否已在队列中（正在处理）
        cur.execute(
            "SELECT id, status FROM import_queue WHERE keyword = %s AND status IN ('pending','processing')",
            (keyword,)
        )
        if cur.fetchone():
            db.close()
            return True  # 正在处理中
        # 检查之前是否已经导入过（2次以上且全部0结果）
        cur.execute(
            "SELECT COUNT(*) as cnt, SUM(result_count) as total FROM import_queue WHERE keyword = %s AND status = 'done'",
            (keyword,)
        )
        row = cur.fetchone()
        if row and row["cnt"] >= 2 and (row["total"] or 0) == 0:
            db.close()
            return "exhausted"  # 已尝试多次，没有资源
        # 检查自动导入是否开启
        cur.execute("SELECT value FROM settings WHERE `key`='auto_import_enabled'")
        row = cur.fetchone()
        if not row or row["value"] != "1":
            db.close()
            return False
        # 加入队列
        cur.execute(
            "INSERT INTO import_queue (keyword, status) VALUES (%s, 'processing')",
            (keyword,)
        )
        qid = cur.lastrowid
        db.commit()
        db.close()
        # 后台线程立即处理
        import threading
        t = threading.Thread(target=_process_import, args=(keyword, qid), daemon=True)
        t.start()
        return True
    except Exception:
        return False


def _fetch_api(api_url, keyword, limit):
    """从单个API搜索资源"""
    import requests as http_requests
    try:
        resp = http_requests.post(api_url, json={"kw": keyword, "limit": limit}, timeout=60)
        data = resp.json()
        if data.get("code") != 0:
            return []
        merged = data.get("data", {}).get("merged_by_type", {})
        results = []
        for source, items in merged.items():
            for item in items:
                results.append({"source": source, "item": item})
        return results
    except Exception:
        return []


def _process_import(keyword, queue_id):
    """后台处理单个导入任务（支持多API并发）"""
    import threading
    try:
        db = get_db()
        cur = db.cursor()
        # 获取API配置（支持多个，换行分隔）
        cur.execute("SELECT value FROM settings WHERE `key`='import_api_url'")
        row = cur.fetchone()
        api_urls_raw = row["value"] if row else "https://ps.aiqinghaiwork.cn/api/search"
        api_urls = [u.strip() for u in api_urls_raw.split("\n") if u.strip()]
        if not api_urls:
            api_urls = [api_urls_raw]

        cur.execute("SELECT value FROM settings WHERE `key`='import_api_limit'")
        row = cur.fetchone()
        limit = int(row["value"]) if row else 30
        db.close()

        # 并发请求所有API
        all_results = []
        if len(api_urls) == 1:
            all_results = _fetch_api(api_urls[0], keyword, limit)
        else:
            threads = []
            results_map = {}
            def fetch_one(url, idx):
                results_map[idx] = _fetch_api(url, keyword, limit)
            for i, url in enumerate(api_urls):
                t = threading.Thread(target=fetch_one, args=(url, i))
                threads.append(t)
                t.start()
            for t in threads:
                t.join(timeout=90)
            for i in range(len(api_urls)):
                all_results.extend(results_map.get(i, []))

        # 去重（按URL）
        seen_urls = set()
        unique_results = []
        for r in all_results:
            url = r["item"].get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_results.append(r)

        # 写入数据库
        db = get_db()
        cur = db.cursor()
        imported = 0
        for r in unique_results:
            item = r["item"]
            source = r["source"]
            cur.execute("""INSERT IGNORE INTO resources
                (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                (keyword, source, item.get("url", ""), item.get("note", "") or item.get("title", ""),
                 item.get("note", ""), item.get("password", ""), item.get("datetime", ""), "", "", 0))
            imported += 1

        cur.execute("UPDATE import_queue SET status='done', result_count=%s WHERE id=%s",
                   (imported, queue_id))
        cur.execute("UPDATE search_logs SET is_imported=1 WHERE keyword=%s", (keyword,))
        db.commit()
        db.close()
    except Exception as e:
        try:
            db = get_db()
            cur = db.cursor()
            cur.execute("UPDATE import_queue SET status='failed', error_msg=%s WHERE id=%s",
                       (str(e)[:200], queue_id))
            db.commit()
            db.close()
        except:
            pass


# ── 用户端：检查导入状态 ──
@app.route("/api/import_status")
def api_import_status():
    """用户搜索后检查是否有自动导入进行中"""
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"importing": False})
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT status, result_count, created_at FROM import_queue "
            "WHERE keyword = %s ORDER BY created_at DESC LIMIT 1",
            (keyword,)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"importing": False})
        return jsonify({
            "importing": row["status"] in ("pending", "processing"),
            "status": row["status"],
            "result_count": row.get("result_count", 0),
            "created_at": str(row["created_at"]) if row.get("created_at") else ""
        })
    finally:
        db.close()



@app.route("/api/admin/import_queue/<int:qid>", methods=["DELETE"])
@admin_required
def api_admin_import_queue_delete(qid):
    """删除单条队列记录"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM import_queue WHERE id = %s", (qid,))
        db.commit()
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()

# ── 后台：手动触发导入队列处理 ──
@app.route("/api/admin/process_queue", methods=["POST"])
@admin_required
def api_admin_process_queue():
    """手动触发处理导入队列"""
    import requests as http_requests
    API_URL = None
    db = get_db()
    try:
        cur = db.cursor()
        # 获取API地址
        cur.execute("SELECT value FROM settings WHERE `key`='import_api_url'")
        row = cur.fetchone()
        API_URL = row["value"] if row else "https://ps.aiqinghaiwork.cn/api/search"
        
        cur.execute("SELECT value FROM settings WHERE `key`='import_api_limit'")
        row = cur.fetchone()
        limit = int(row["value"]) if row else 30

        # 获取待处理任务
        cur.execute("SELECT * FROM import_queue WHERE status = 'pending' ORDER BY created_at LIMIT 10")
        tasks = cur.fetchall()
        
        processed = 0
        for task in tasks:
            kw = task["keyword"]
            try:
                cur.execute("UPDATE import_queue SET status='processing' WHERE id=%s", (task["id"],))
                db.commit()
                
                resp = http_requests.post(API_URL, json={"kw": kw, "limit": limit}, timeout=60)
                api_data = resp.json()
                if api_data.get("code") != 0:
                    cur.execute("UPDATE import_queue SET status='failed', error_msg=%s WHERE id=%s",
                               (api_data.get("msg", "API错误"), task["id"]))
                    db.commit()
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
                
                cur.execute("UPDATE import_queue SET status='done', result_count=%s WHERE id=%s",
                           (imported, task["id"]))
                # 同时标记search_logs
                cur.execute("UPDATE search_logs SET is_imported=1 WHERE keyword=%s", (kw,))
                db.commit()
                processed += 1
            except Exception as e:
                cur.execute("UPDATE import_queue SET status='failed', error_msg=%s WHERE id=%s",
                           (str(e)[:200], task["id"]))
                db.commit()
        
        # 清除缓存
        for k in list(_cache.keys()):
            if "/api/" in k:
                del _cache[k]
        
        return jsonify({"ok": True, "processed": processed, "total_pending": len(tasks)})
    finally:
        db.close()




@app.route("/api/admin/logs/clear", methods=["POST"])
@admin_required
def api_admin_logs_clear():
    """清空操作日志"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM operation_logs")
        db.commit()
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()

# ==================== API 测试工具 ====================
@app.route("/api/admin/test_apis")
@admin_required
def api_admin_test_apis():
    """测试所有API端点是否正常"""
    import time as _time
    results = []
    db = get_db()
    try:
        cur = db.cursor()
        
        # Test each API
        tests = [
            ("GET", "/api/stats", lambda: cur.execute("SELECT COUNT(*) as c FROM resources")),
            ("GET", "/api/sources", lambda: cur.execute("SELECT DISTINCT source FROM resources LIMIT 1")),
            ("GET", "/api/admin/settings", lambda: cur.execute("SELECT COUNT(*) as c FROM settings")),
            ("GET", "/api/admin/import_queue", lambda: cur.execute("SELECT COUNT(*) as c FROM import_queue")),
            ("GET", "/api/admin/stats/dashboard", lambda: cur.execute("SELECT COUNT(*) as c FROM resources")),
            ("GET", "/api/admin/resources", lambda: cur.execute("SELECT COUNT(*) as c FROM resources")),
            ("GET", "/api/admin/pending_imports", lambda: cur.execute("SELECT COUNT(*) as c FROM search_logs WHERE is_imported=0")),
            ("GET", "/api/admin/logs", lambda: cur.execute("SELECT COUNT(*) as c FROM operation_logs")),
            ("GET", "/api/admin/filter_words", lambda: cur.execute("SELECT COUNT(*) as c FROM filter_words")),
            ("GET", "/api/tmdb/trending", lambda: None),  # External API
            ("GET", "/api/hot_searches", lambda: cur.execute("SELECT keyword FROM search_logs LIMIT 1")),
            ("GET", "/api/autocomplete?q=test", lambda: cur.execute("SELECT 1")),
            ("GET", "/api/docs", lambda: None),
        ]
        
        for method, path, db_check in tests:
            start = _time.time()
            try:
                db_check()
                elapsed = round((_time.time() - start) * 1000, 1)
                results.append({"method": method, "path": path, "status": "ok", "ms": elapsed})
            except Exception as e:
                elapsed = round((_time.time() - start) * 1000, 1)
                results.append({"method": method, "path": path, "status": "error", "error": str(e)[:100], "ms": elapsed})
        
        # DB connection test
        start = _time.time()
        cur.execute("SELECT 1")
        db_ms = round((_time.time() - start) * 1000, 1)
        
        # Table stats
        tables = {}
        for table in ["resources", "users", "search_logs", "favorites", "filter_words", "settings", "import_queue", "operation_logs"]:
            try:
                cur.execute(f"SELECT COUNT(*) as c FROM {table}")
                tables[table] = cur.fetchone()["c"]
            except:
                tables[table] = -1
        
        return jsonify({
            "results": results,
            "db_latency_ms": db_ms,
            "tables": tables,
            "total_ok": sum(1 for r in results if r["status"] == "ok"),
            "total_error": sum(1 for r in results if r["status"] == "error"),
        })
    finally:
        db.close()



# ==================== 失效链接检测 ====================
@app.route("/api/admin/deadlinks/scan", methods=["POST"])
@admin_required
def api_admin_deadlinks_scan():
    """扫描失效链接（网盘链接，排除磁力/电驴）- 使用各网盘API精准检测"""
    import requests as http_requests

    data = request.get_json() or {}
    source_filter = data.get("source", "")
    limit = min(int(data.get("limit", 50)), 200)
    page = int(data.get("page", 1))
    skip_checked = data.get("skip_checked", True)  # 默认跳过已检测
    only_unchecked = data.get("only_unchecked", False)  # 只检测未检测的

    db = get_db()
    try:
        cur = db.cursor()
        exclude_sources = "('magnet', 'ed2k', 'other')"
        conditions = [f"source NOT IN {exclude_sources}"]
        params = []

        if source_filter:
            conditions.append("source = %s")
            params.append(source_filter)

        if only_unchecked:
            conditions.append("last_checked IS NULL")
        elif skip_checked:
            conditions.append("(last_checked IS NULL OR last_checked < DATE_SUB(NOW(), INTERVAL 7 DAY))")

        where = " AND ".join(conditions)
        offset = (page - 1) * limit

        cur.execute(
            f"SELECT id, title, source, url, password FROM resources "
            f"WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        items = cur.fetchall()

        cur.execute(f"SELECT COUNT(*) as c FROM resources WHERE {where}", params)
        total = cur.fetchone()["c"]

        results = []
        dead_count = 0
        alive_count = 0
        unknown_count = 0

        def _extract_id(url, source):
            """从URL提取分享ID"""
            import re
            if "quark" in source:
                m = re.search(r'/s/([a-zA-Z0-9]+)', url)
                return m.group(1) if m else None
            elif "baidu" in source or "bdpan" in source:
                m = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
                return m.group(1) if m else None
            elif "aliyun" in source or "alipan" in source:
                m = re.search(r'/s/([a-zA-Z0-9]+)', url)
                return m.group(1) if m else None
            elif "xunlei" in source:
                m = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
                return m.group(1) if m else None
            elif "115" in source:
                m = re.search(r'/s/([a-zA-Z0-9]+)', url)
                return m.group(1) if m else None
            elif "pikpak" in source:
                m = re.search(r'/s/([a-zA-Z0-9]+)', url)
                return m.group(1) if m else None
            return None

        def _check_link(url, source):
            """根据网盘类型使用API精准检测"""
            share_id = _extract_id(url, source)
            if not share_id:
                return "unknown", 0, "无法提取分享ID"

            try:
                # 夸克网盘
                if "quark" in source:
                    r = http_requests.post(
                        "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token",
                        json={"pwd_id": share_id, "passcode": ""},
                        timeout=10, headers={"User-Agent": "Mozilla/5.0"}
                    )
                    if r.status_code == 404:
                        msg = r.json().get("message", "")
                        if "失效" in msg or "不存在" in msg:
                            return "dead", 404, msg
                    return "alive", r.status_code, ""

                # 百度网盘
                elif "baidu" in source or "bdpan" in source:
                    r = http_requests.post(
                        "https://pan.baidu.com/share/verify",
                        data={"surl": share_id, "pwd": ""},
                        timeout=10, headers={"User-Agent": "Mozilla/5.0"}
                    )
                    d = r.json()
                    errno = d.get("errno", -1)
                    if errno == -12 or errno == -9:
                        return "dead", r.status_code, "分享已失效"
                    elif errno == 0 or errno == -6:
                        return "alive", r.status_code, ""
                    return "unknown", r.status_code, f"errno={errno}"

                # 阿里云盘
                elif "aliyun" in source or "alipan" in source:
                    # 先提取真实share_id (alipan.com/s/xxx 中的xxx)
                    r = http_requests.post(
                        "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous",
                        json={"share_id": share_id},
                        timeout=10, headers={"User-Agent": "Mozilla/5.0"}
                    )
                    if r.status_code == 404:
                        msg = r.json().get("message", "")
                        if "not found" in msg.lower() or "cannot" in msg.lower():
                            return "dead", 404, msg
                    elif r.status_code == 200:
                        return "alive", 200, ""
                    return "unknown", r.status_code, ""

                # 迅雷/115/pikpak/其他 - 用HTTP GET检查页面
                else:
                    r = http_requests.get(url, timeout=10, allow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0"})
                    body = r.text[:2000].lower()
                    dead_words = ['已失效', '已过期', '已删除', '不存在', '取消分享', '违规',
                                  '页面不存在', '资源不存在', '分享已过期', '链接已失效',
                                  '文件已删除', '该分享已过期', '此分享已过期', 'expired',
                                  'removed', 'not found']
                    for word in dead_words:
                        if word in body:
                            return "dead", r.status_code, f"页面包含'{word}'"
                    if r.status_code == 200:
                        return "alive", 200, ""
                    return "unknown", r.status_code, ""

            except http_requests.exceptions.Timeout:
                return "timeout", 0, "请求超时"
            except http_requests.exceptions.ConnectionError:
                return "dead", 0, "连接失败"
            except Exception as e:
                return "error", 0, str(e)[:50]

        for item in items:
            url = item.get("url", "")
            source = (item.get("source") or "").lower()

            if not url:
                status, code, msg = "invalid", 0, "空链接"
                unknown_count += 1
            elif url.startswith("magnet:") or url.startswith("ed2k:"):
                status, code, msg = "skip", 0, ""
            else:
                status, code, msg = _check_link(url, source)
                if status == "dead":
                    dead_count += 1
                elif status == "alive":
                    alive_count += 1
                else:
                    unknown_count += 1

            results.append({
                "id": item["id"],
                "title": (item.get("title") or "")[:80],
                "source": item.get("source", ""),
                "url": url[:100],
                "password": item.get("password", ""),
                "status": status,
                "status_code": code,
                "msg": msg,
            })

            # 标记已检测
            if status not in ("skip", "invalid"):
                try:
                    cur.execute(
                        "UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s",
                        (status, item["id"])
                    )
                except Exception:
                    pass

        db.commit()  # 提交所有标记更新

        return jsonify({
            "results": results,
            "total": total,
            "page": page,
            "limit": limit,
            "dead_count": dead_count,
            "alive_count": alive_count,
            "unknown_count": unknown_count,
            "scanned": len(items),
        })
    finally:
        db.close()


@app.route("/api/admin/deadlinks/delete", methods=["POST"])
@admin_required
def api_admin_deadlinks_delete():
    """批量删除失效链接"""
    data = request.get_json() or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "请选择要删除的链接"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", ids)
        db.commit()
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()


@app.route("/api/admin/deadlinks/sources")
@admin_required
def api_admin_deadlinks_sources():
    """获取可检测的网盘来源列表"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT source, COUNT(*) as c FROM resources "
            "WHERE source NOT IN ('magnet', 'ed2k', 'other') "
            "GROUP BY source ORDER BY c DESC"
        )
        items = cur.fetchall()
        return jsonify({"items": items})
    finally:
        db.close()



@app.route("/api/admin/deadlinks/unchecked_count")
@admin_required
def api_admin_deadlinks_unchecked_count():
    """获取未检测链接数量"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT source, COUNT(*) as c FROM resources "
            "WHERE source NOT IN ('magnet', 'ed2k', 'other') AND last_checked IS NULL "
            "GROUP BY source ORDER BY c DESC"
        )
        items = cur.fetchall()
        total = sum(i["c"] for i in items)
        return jsonify({"items": items, "total": total})
    finally:
        db.close()

# ==================== API Docs Page ====================

@app.route("/api-docs")
@app.route("/api-docs.html")
def api_docs_page():
    return send_from_directory(".", "api-docs.html")


# ==================== Favicon ====================

@app.route("/favicon.ico")
def favicon():
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
