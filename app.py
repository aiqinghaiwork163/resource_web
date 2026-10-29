#!/usr/bin/env python3
"""
网盘资源浏览 Web 应用 v3.0
Flask + MySQL，支持连接池、admin认证、搜索限流
"""
import os
import re
import time
import hashlib
import secrets
import html as html_mod
from functools import wraps
from datetime import datetime, timedelta

import pymysql
import pymysql.cursors
import requests as http_requests
from flask import Flask, jsonify, request, send_from_directory, session, redirect
from requests_oauthlib import OAuth2Session
import json
import bcrypt
import hashlib

from flask_wtf.csrf import CSRFProtect, generate_csrf

# ==================== Redis 缓存 ====================







# ==================== 密码哈希工具（bcrypt） ====================


from blueprints.common import DB_CONFIG, _get_setting
app = Flask(__name__)
# secret_key 在 _get_setting() 定义后设置（见下方）
# 与 :5001 生产站隔离 Cookie，避免同主机登录会话互相覆盖
app.config['SESSION_COOKIE_NAME'] = os.environ.get('SESSION_COOKIE_NAME', 'session')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# 全站经 Cloudflare Tunnel 以 HTTPS 提供，Secure cookie 安全
app.config['SESSION_COOKIE_SECURE'] = True

# ==================== 日志轮转 ====================
# 落盘到 /app/logs/app.log（卷挂载持久化），RotatingFileHandler 防止无限增长
try:
    import logging as _logging
    from logging.handlers import RotatingFileHandler as _RotatingFileHandler
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _fh = _RotatingFileHandler(
        os.path.join(_log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
        delay=True,
    )
    _fh.setFormatter(_logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    app.logger.addHandler(_fh)
    app.logger.setLevel(_logging.INFO)
except Exception as _log_e:
    app.logger.warning("日志轮转初始化失败: %s", _log_e)

# ==================== CSRF 保护 ====================
# 允许 API 端点通过 Header 传递 CSRF token
app.config['WTF_CSRF_HEADERS'] = ['X-CSRFToken', 'X-CSRF-Token']

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
        # 已经压缩过的不再压缩（防止双重gzip）
        if response.headers.get("Content-Encoding"):
            return response
        accept = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accept:
            return response
        ct = response.content_type or ""
        if not any(t in ct for t in ["json", "html", "javascript", "css", "text"]):
            return response
        # send_from_directory 文件响应 direct_passthrough=True 且 is_streamed=True（FileWrapper 无 len），
        # 必须先判 passthrough：可压缩类型且体积可控（<=2MB）时关闭 passthrough 后缓冲压缩；
        # 其余真流式响应（生成器等）跳过。
        if response.direct_passthrough:
            _clen = response.content_length
            if _clen is None or _clen > 2 * 1024 * 1024:
                return response
            response.direct_passthrough = False
        elif response.is_streamed:
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
        # 压缩后原 ETag / Accept-Ranges 描述的是未压缩实体，移除以免缓存/断点续传错乱
        if "ETag" in response.headers:
            del response.headers["ETag"]
        if "Accept-Ranges" in response.headers:
            del response.headers["Accept-Ranges"]
    except (RuntimeError, TypeError):
        app.logger.exception("add_gzip failed")
        pass  # send_from_directory 直接流式响应，跳过压缩
    return response


# ==================== 图片代理缓存 ====================
import hashlib
import threading
from collections import OrderedDict




# 图片代理白名单（精确域名后缀匹配；校验见 _img_proxy_url_ok）






# ==================== 静态文件长缓存 ====================
@app.after_request
def cache_static(response):
    """静态文件长缓存；API 响应禁止缓存（防止登录态/动态数据被浏览器/CDN 缓存）"""
    if request.path.startswith("/api/img_proxy"):
        return response  # 图片代理自带 ETag + immutable 缓存头，不覆盖
    if request.path.startswith("/static/") and response.status_code == 200:
        # 字体/CSS/JS 等静态资源长缓存（30 天）；/static 下的 HTML 不在此列（send_file 自带 no-cache）
        _ext = request.path.rsplit(".", 1)[-1].lower()
        if _ext in ("css", "js", "woff", "woff2", "ttf", "otf", "svg", "png", "jpg", "jpeg", "gif", "webp", "ico"):
            response.headers["Cache-Control"] = "public, max-age=2592000"
        return response
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
    if request.path.startswith("/login"):
        return response
    if response.status_code == 200 and not response.headers.get("Cache-Control"):
        ct = response.content_type or ""
        if "html" in ct:
            # HTML 入口文件用协商缓存：部署新版后用户即时生效，避免 300 秒旧界面
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers["Cache-Control"] = "public, max-age=300"
    return response



@app.after_request
def add_security_headers(response):
    """添加安全响应头：HSTS / CSP / 防嗅探 / 防嵌框"""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # 现代浏览器已弃用 X-XSS-Protection，留着反而可能引入反射XSS问题，移除
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # CSP：默认仅同源，挡 XSS / 注入；图片允许 tmdb/douban 代理域名
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' https://image.tmdb.org https://*.doubanio.com data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'"
    )
    return response


# ==================== 配置 ====================

# ==================== 限流 ====================
from dbutils.pooled_db import PooledDB





from contextlib import contextmanager

@contextmanager
def db_session():
    """数据库会话上下文管理器：统一 cursor 获取/commit/异常回滚/连接归还。

    消除散落在 60+ 路由里的 db=get_db(); try: ... finally: db.close() 样板。
    用法:
        with db_session() as cur:
            cur.execute(...)
            rows = cur.fetchall()
    语义: 正常退出自动 commit；抛异常自动 rollback 并向上抛；连接始终归还池。
    """
    db = get_db()
    try:
        cur = db.cursor()
        yield cur
        db.commit()
    except Exception:
        app.logger.exception("db_session failed")
        try:
            db.rollback()
        except Exception:
            app.logger.exception("db_session failed")
            pass
        raise
    finally:
        db.close()






# 设置 secret_key（_get_setting 可用后）
app.secret_key = _get_setting("secret_key") or os.environ.get("SECRET_KEY")

ADMIN_USER = _get_setting("admin_user") or os.environ.get("ADMIN_USER")
ADMIN_PASS = _get_setting("admin_pass") or os.environ.get("ADMIN_PASS")

if not ADMIN_USER or not ADMIN_PASS or not app.secret_key:
    raise RuntimeError("环境变量 ADMIN_USER、ADMIN_PASS 和 SECRET_KEY 必须设置")

if not os.environ.get("DB_PASSWORD"):
    raise RuntimeError("环境变量 DB_PASSWORD 必须设置")

# ==================== 限流 ====================
# 注意: 限流状态必须放 Redis，不能放进程内存。gunicorn 跑 4 个 worker 是
# 独立进程，各算各的计数，实际阈值会被放大 4 倍（登录限流 5 次变成 20 次），
# 等于把暴力破解成本降低 4 倍。Redis 不可用时降级为进程内存 + Lock 兜底，
# 保证限流永远不会完全失效。
import threading as _threading





# ==================== 登录限流 ====================










# ==================== 内存缓存 ====================
_CACHE_MAX_SIZE = 500  # 最大缓存条目数，防止内存泄漏


def _cleanup_cache():
    """清理过期条目，必要时淘汰最旧条目以控制内存"""
    now = time.time()
    expired = [k for k, (_, expire) in _cache.items() if now >= expire]
    for k in expired:
        _cache.pop(k, None)
    if len(_cache) > _CACHE_MAX_SIZE:
        # FIFO 淘汰，只保留最新的一半
        keep = list(_cache.items())[-_CACHE_MAX_SIZE // 2:]
        _cache.clear()
        _cache.update(keep)


def cached(ttl=300):
    """简单内存缓存装饰器，ttl秒内返回缓存数据"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = request.url
            now = time.time()
            if len(_cache) > _CACHE_MAX_SIZE:
                _cleanup_cache()
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






# ==================== Admin 认证 ====================










def _is_admin_role(role) -> bool:
    """普通管理员（不含超管）"""
    return _norm_role(role) == "admin"






# ==================== 登录门禁（主页可逛，搜索/详情/收藏要登录）====================
# 前台搜索/片源暂不强制登录；REQUIRE_LOGIN=1 可重新打开
REQUIRE_LOGIN = os.environ.get("REQUIRE_LOGIN", "0").strip().lower() not in (
    "0", "false", "no", "off",
)

# 未登录可访问的前缀（主页逛海报 + 登录链路）
_LOGIN_PUBLIC_PREFIXES = (
    "/login",
    "/logout",
    "/favicon",
    "/static/",
    "/manifest.webmanifest",
    "/sw.js",
    "/site-settings.js",
    "/api/user/me",
    "/api/csrf-token",
    "/api/health",
    "/api/site-settings",
    "/api/tmdb/",
    "/api/img_proxy",
    "/api/announcements",
    "/api/stats",
    "/api/telegram/login-config",
    "/api/auth/telegram/",
    "/api/telegram/bot-webhook/",
    "/api/visit",
    "/api/growth",
)




@app.before_request
def require_login_gate():
    """未登录：主页/海报/登录相关放行；搜索与片源等 API 返回 401；个人中心跳登录页。"""
    if not REQUIRE_LOGIN:
        return None
    if session.get("user_id"):
        return None
    if request.method == "OPTIONS":
        return None
    path = request.path or "/"
    if path == "/":
        return None
    for prefix in _LOGIN_PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return None
    # 管理后台仍由 admin_required 处理（未登录也会 401/跳登录）
    if path.startswith("/api/"):
        next_url = _safe_next_url(request.args.get("next") or "/")
        return jsonify({
            "error": "请先登录后再使用搜索与片源",
            "login_required": True,
            "login_url": f"/login?next={next_url}",
        }), 401
    if path.startswith("/profile") or path.startswith("/admin"):
        return redirect(f"/login?next={_safe_next_url(path)}")
    return None


def _is_guest():
    """未登录访客判定（与 require_login_gate 同一口径）。"""
    return not session.get("user_id")




def login_required(f):
    """路由级登录校验（可选，全局 before_request 已覆盖大部分）。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not REQUIRE_LOGIN or session.get("user_id"):
            return f(*args, **kwargs)
        if request.is_json or request.path.startswith("/api/"):
            return jsonify({"error": "请先登录", "login_required": True}), 401
        return redirect(f"/login?next={_safe_next_url(request.path)}")
    return decorated




# ==================== OAuth 2.0 (GitHub) ====================










def _get_oauth_github():
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return None
    redirect_uri = _build_oauth_redirect_uri()
    return OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=redirect_uri,
        scope=["user:email"],
    )






# ==================== Telegram Login Widget ====================
# 硬编码兜底超管 TG ID；后台 settings.telegram_superadmin_ids 可追加（逗号分隔）


































































# ==================== Telegram Bot 绑定（深链 /start bind_xxx） ====================






def _telegram_api_proxies():
    return _oauth_http_proxies()










def _sanitize_ft_query(q: str):
    q = re.sub(r"[+\-<>()~*\"'@]", " ", (q or "").strip())[:80]
    parts = [p for p in re.split(r"\s+", q) if p][:5]
    if not parts:
        return "", ""
    display = " ".join(parts)
    return display, " ".join(f"+{p}*" for p in parts)
















































# ==================== 主页 ====================


# ==================== API: 统计 ====================
# ── 用户信息 API ──








# ==================== 累计访问 ====================
_VISIT_DAILY_PREFIX = "res_web:visits:daily:"












# ==================== API: 网盘分类标签 ====================


# ==================== API: 资源列表 ====================


# ==================== API: 来源统计 ====================











# ==================== API: 关键词统计 ====================


# ==================== API: 数据导入（带限流） ====================


def _parse_online_items(keyword, api_data):
    """解析 pansou API 返回数据为标准 items 列表"""
    merged = api_data.get("data", {}).get("merged_by_type", {})
    items = []
    for src, lst in merged.items():
        for it in lst:
            items.append({
                "id": None,
                "keyword": keyword,
                "source": src,
                "url": it.get("url", ""),
                "title": it.get("note", "") or it.get("title", ""),
                "note": it.get("note", ""),
                "password": it.get("password", ""),
                "quality": it.get("quality", ""),
                "type": it.get("type", ""),
                "year": it.get("year", 0),
                "rating": None,
                "datetime": it.get("datetime", ""),
                "created_at": None,
            })
    return items
















# ==================== CSRF Token ====================

# ==================== API: 健康检查 ====================


# ==================== 管理页面 ====================


# ==================== 用户管理 API ====================

















# ==================== 积分与签到管理接口 ====================











# ==================== 官方邮箱验证码与邮箱注册 ====================










# ==================== API: 公告管理 ====================










# ==================== API: 资源来源管理 ====================








# ==================== 用户信息 API ====================

# ==================== 修改密码（任意登录用户） ====================

# ==================== 收藏 API ====================




# ==================== TMDB 海报片单（首页海报墙收藏） ====================









# ==================== 搜索历史 API ====================



# ==================== 点击统计 ====================
# ==================== 个人中心页面 ====================


# ==================== 搜索日志收集（所有搜索） ====================




# ==================== 管理员：待导入搜索词 ====================


# ==================== 管理员：确认导入 ====================


# ==================== 管理员：操作日志 ====================


# ==================== 过滤词管理 ====================





# ==================== 新功能 API ====================

# ── ① 热门搜索排行 ──


# ── ② 搜索自动补全 ──


# ── ③ 资源详情面板 ──
# ==================== 众包死链报错 ====================




# ── ④ TMDB 热门排行 ──



# ── ④ 推荐系统 ──


# ── ⑤ 关键词订阅 ──






# ── ⑧ 资源分享 ──




# ── ⑨ 后台数据统计面板 ──


# ── ⑩ API 文档 ──



# ==================== 后台管理增强 API ====================

# ── 系统设置 ──




# ── 资源批量操作 ──






# ── 资源批量操作 ──


# ── 资源列表（后台增强版） ──








# ── 自动导入队列 ──




# ── 自动导入逻辑（搜索0结果时自动触发并处理） ──
def _auto_import_trigger(keyword):
    """搜索时自动加入队列并立即后台处理。返回值: True=导入中, 'exhausted'=已尝试过无结果, False=不触发"""
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
        # 检查最近是否已经成功导入过（1小时内有结果的不重复导入）
        cur.execute(
            "SELECT id FROM import_queue WHERE keyword = %s AND status = 'done' AND result_count > 0 AND updated_at > DATE_SUB(NOW(), INTERVAL 1 HOUR) LIMIT 1",
            (keyword,)
        )
        if cur.fetchone():
            db.close()
            return False  # 最近已成功导入，跳过
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
        app.logger.exception("_auto_import_trigger failed")
        return False


# ── 自动检测新导入链接有效性 ──
def _auto_check_links_bg(items):
    """后台线程池并发：批量检测新导入资源的链接有效性"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    def _worker(items):
        if not items:
            return
        
        def _check_and_update(item):
            try:
                url = item.get("url") or ""
                source = (item.get("source") or "").lower()
                rid = item.get("id")
                if not url or not rid:
                    return
                status, code, msg = _check_single_link(url, source)
                db = get_db()
                try:
                    cur = db.cursor()
                    cur.execute("UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s", (status, rid))
                    db.commit()
                finally:
                    db.close()
            except Exception:
                app.logger.exception("_check_and_update failed")
                pass

        with ThreadPoolExecutor(max_workers=10) as pool:
            pool.map(_check_and_update, items)
        _invalidate_resource_cache()

    t = threading.Thread(target=_worker, args=(items,), daemon=True)
    t.start()




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
        app.logger.exception("_fetch_api failed")
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
        api_urls_raw = row["value"] if row else "https://pansou.42078207.qzz.io/api/search\nhttps://pansou.42078207.xyz/api/search"
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
        try:
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
        finally:
            db.close()

        # 自动检测新导入资源的链接有效性
        if imported > 0:
            _auto_scan_unchecked_bg(min(imported, 50))
    except Exception as e:
        app.logger.exception("_process_import failed")
        try:
            db = get_db()
            try:
                cur = db.cursor()
                cur.execute("UPDATE import_queue SET status='failed', error_msg=%s WHERE id=%s",
                           (str(e)[:200], queue_id))
                db.commit()
            finally:
                db.close()
        except:
            app.logger.exception("_process_import failed")
            pass


# ── 用户端：检查导入状态 ──





# ── 后台：手动触发导入队列处理 ──





# ==================== API 测试工具 ====================



# ==================== 失效链接检测 ====================


def _extract_id(url, source):
    """从URL提取分享ID"""
    u = (url or "").lower()
    if "quark" in u or "pan.quark.cn" in u:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    elif "baidu" in u or "pan.baidu.com" in u:
        m = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
        return m.group(1) if m else None
    elif "aliyun" in u or "alipan" in u or "aliyundrive" in u:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    elif "xunlei" in u or "pan.xunlei.com" in u:
        m = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
        return m.group(1) if m else None
    elif "115" in u or "115.com" in u:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    elif "pikpak" in u or "mypikpak" in u:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    return None
















# ==================== API Docs Page ====================



# ==================== TMDB Crawler ====================






def _tmdb_crawler_worker():
    """TMDB 爬虫后台工作线程"""
    import socket as _sock
    import ssl as _ssl
    import requests as _req
    import json as _json

    TMDB_KEY = API_KEY = _get_tmdb_api_key()
    SEARCH_URL = _get_import_api_urls()[0]
    endpoints = {
        "趋势(日)": "trending/all/day",
        "趋势(周)": "trending/all/week",
        "电影-热门": "movie/popular",
        "电影-影院": "movie/now_playing",
        "电影-即将": "movie/upcoming",
        "剧集-热门": "tv/popular",
        "剧集-今日播出": "tv/airing_today",
        "剧集-正在播出": "tv/on_the_air",
    }

    def log(m):
        ts = datetime.now().strftime("%H:%M:%S")
        _tmdb_crawler_status["log"].append(f"[{ts}] {m}")

    def tmdb_get(ep, p=1):
        try:
            ip = _sock.getaddrinfo("www.themoviedb.org", 443, _sock.AF_INET)[0][4][0]
            path = f"/3/{ep}?api_key={TMDB_KEY}&language=zh-CN&page={p}"
            s = _sock.create_connection((ip, 443), 10)
            ss = _ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
            ss.sendall(f"GET {path} HTTP/1.1\r\nHost: api.themoviedb.org\r\nAccept: application/json\r\nConnection: close\r\n\r\n".encode())
            r = b""
            while True:
                c = ss.recv(65536)
                if not c: break
                r += c
            ss.close()
            h = r.find(b"\r\n\r\n")
            if h < 0: return None
            body = r[h+4:]
            if b"chunked" in r[:h]:
                lines = body.split(b"\r\n")
                decoded = b""
                for line in lines:
                    if line and not all(c in b"0123456789abcdefABCDEF" for c in line[:8]):
                        decoded += line
                body = decoded
            return _json.loads(body.decode())
        except:
            app.logger.exception("tmdb_get failed")
            return None

    try:
        # Phase 1: Fetch TMDB titles
        log("📡 Phase 1: 获取 TMDB 热门标题...")
        titles = []
        seen = set()
        for label, ep in endpoints.items():
            log(f"  [{label}] ...")
            data = tmdb_get(ep)
            if not data:
                log(f"  ✗ 获取失败")
                continue
            count = 0
            for item in data.get("results", []):
                t = (item.get("title") or item.get("name", "")).strip()
                k = t.lower()
                if t and k not in seen:
                    seen.add(k)
                    titles.append(t)
                    count += 1
            log(f"  +{count} (共{len(seen)})")
            time.sleep(0.3)

        _tmdb_crawler_status["total"] = len(titles)
        log(f"📊 共{len(titles)}个标题\n")

        # Phase 2: Search and import
        log("🔍 Phase 2: 搜索网盘资源并导入...")
        db = get_db()
        cur = db.cursor()
        imported = 0

        for i, title in enumerate(titles, 1):
            _tmdb_crawler_status["progress"] = i
            log(f"[{i}/{len(titles)}] 🔍 {title}")
            try:
                resp = _req.post(SEARCH_URL, json={"kw": title, "limit": 30}, timeout=60)
                result = resp.json()
                if result.get("code") != 0:
                    continue
                merged = result.get("data", {}).get("merged_by_type", {})
                items = []
                for src, its in merged.items():
                    items.extend(its)
                if not items:
                    log("  ℹ️ 无结果")
                    continue

                added = 0
                nw = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for it in items:
                    url = it.get("url", "")
                    if not url:
                        continue
                    cur.execute("SELECT 1 FROM resources WHERE url=%s", (url,))
                    if cur.fetchone():
                        continue
                    title_text = it.get("note") or it.get("title") or title
                    src = (it.get("source") or "other").lower()
                    cur.execute(
                        "INSERT INTO resources(url,title,keyword,password,source,datetime,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (url, title_text, title, it.get("password", ""), src, nw, nw)
                    )
                    added += 1
                db.commit()
                imported += added
                _tmdb_crawler_status["imported"] = imported
                log(f"  ✅ +{added}" if added else "  ℹ️ 已存在")
            except Exception as e:
                app.logger.warning("_tmdb_crawler_worker failed: %s", e)
                log(f"  ✗ 错误: {str(e)[:60]}")
            time.sleep(0.3)

        cur.close()
        db.close()
        log(f"\n🎉 完成！共{len(titles)}个标题，新增{imported}条资源")
    except Exception as e:
        app.logger.exception("_tmdb_crawler_worker failed")
        log(f"\n❌ 爬虫异常: {e}")
    finally:
        _tmdb_crawler_status["running"] = False


# ==================== Multi-Source Crawler ====================






# ==================== OMDb Rating Crawler ====================






# ==================== 主页增强 API ====================









# ==================== 数据清洗与分类归一化 ====================





# ==================== 去重 / 自动清理 / 订阅推送 ====================













# ==================== Static Files ====================











# ==================== SEO: robots + sitemap ====================





# -- P2-10: blueprint registration (frontend/auth/admin/crawler) --
from blueprints.common import GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, _auto_scan_unchecked_bg, _build_oauth_redirect_uri, _cache, _check_single_link, _get_cached_filter_words, _get_import_api_urls, _get_tmdb_api_key, _growth_info, _invalidate_resource_cache, _norm_role, _oauth_http_proxies, _safe_next_url, _setting_db, _telegram_api, _telegram_bot_token, _telegram_required_chat, _telegram_superadmin_ids, _telegram_webhook_base, _tmdb_crawler_status, _user_pass_internal_beta_gate, get_db, get_redis, csrf
csrf.init_app(app)
from blueprints import admin_bp, auth_bp, crawler_bp, frontend_bp
app.register_blueprint(frontend_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(crawler_bp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)


# ──────────────── 全局后台死链巡检控制器 ────────────────
import threading
from concurrent.futures import ThreadPoolExecutor










# ── 任务队列中心 API ──





