"""Shared helpers moved from app.py (P2-10 blueprint split).





`app` is a flask.current_app proxy: identical behavior inside requests.


csrf uses deferred init: app.py calls csrf.init_app(app) at bootstrap.


"""

from flask import current_app as app
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, send_from_directory, session, redirect
from flask_wtf.csrf import CSRFProtect, generate_csrf
from functools import wraps
import bcrypt
import hashlib
import json
import os
import pymysql
import re
import requests as http_requests
import secrets
import threading
import time
from datetime import datetime, timedelta
from dbutils.pooled_db import PooledDB
import threading as _threading































def get_redis():
    global _redis_client
    if _redis_client is None:
        import redis
        _redis_client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            db=int(os.environ.get("REDIS_DB", "0")),
            password=os.environ.get("REDIS_PASSWORD") or _get_setting("redis_password"),
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _redis_client


def _make_cache_key(*args):
    """生成 Redis 缓存 key，保留第一个参数作为可扫描的前缀。

    例如 _make_cache_key("resources", q, page) → res_web:resources:<md5>
    前缀化后 _invalidate_resource_cache 才能用 scan_iter("res_web:resources:*")
    精确清掉对应缓存（旧版本 key 无前缀，失效逻辑形同虚设）。
    """
    raw = "|".join(str(a) for a in args)
    prefix = str(args[0]) if args else "k"
    return f"res_web:{prefix}:" + hashlib.md5(raw.encode()).hexdigest()


def _json_default(o):
    """JSON 序列化兜底：datetime/date/Decimal 等非原生类型转字符串。

    注意: 缓存写入用的是标准库 json.dumps，它不像 Flask 的 jsonify 那样
    支持 datetime。缺少这个 default 会让 json.dumps 抛 TypeError，
    进而被调用处的 except 静默吞掉，导致缓存永远写不进去。
    """
    import datetime as _dt
    import decimal as _dec
    if isinstance(o, (_dt.datetime, _dt.date, _dt.time)):
        return o.isoformat()
    if isinstance(o, _dt.timedelta):
        return o.total_seconds()
    if isinstance(o, _dec.Decimal):
        return float(o)
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    return str(o)


def _invalidate_resource_cache():
    """导入/删除资源后清缓存：resources 列表 + stats/keywords/hot_searches 统计缓存"""
    try:
        r = get_redis()
        # 清资源列表缓存
        for key in r.scan_iter("res_web:resources:*"):
            r.delete(key)
        # 清 COUNT 缓存（写库后 total 会变）
        for key in r.scan_iter("res_web:rcount:*"):
            r.delete(key)
        # 清统计类缓存（导入后 total/types/keywords/hot 都会变）
        for prefix in ("res_web:stats:", "res_web:keywords:", "res_web:hot_searches:"):
            for key in r.scan_iter(prefix + "*"):
                r.delete(key)
    except Exception:
        app.logger.exception("_invalidate_resource_cache failed")
        pass


def hash_password(password: str) -> str:
    """使用 bcrypt 对密码进行哈希"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, stored_hash: str) -> bool:
    """验证密码，兼容旧版 SHA256 哈希（迁移期）"""
    if not stored_hash:
        return False
    # 新格式：bcrypt 哈希以 $2b$ 开头
    if stored_hash.startswith('$2b$') or stored_hash.startswith('$2a$'):
        return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))
    # 旧格式：SHA256 hex（64位十六进制字符串）
    if len(stored_hash) == 64 and all(c in '0123456789abcdef' for c in stored_hash.lower()):
        old_hash = hashlib.sha256(password.encode()).hexdigest()
        if old_hash == stored_hash:
            # 验证成功，自动迁移到 bcrypt
            return True
        return False
    return False


csrf = CSRFProtect()


_img_cache = OrderedDict()  # url -> (data, content_type, expire)


_IMG_TTL = 604800  # 7 天


_img_cache_lock = threading.Lock()  # gunicorn 每 worker 2 线程，OrderedDict 需要保护


def _img_cache_evict():
    """淘汰最早的缓存条目，保持 _img_cache 不超过 _IMG_CACHE_MAX"""
    while len(_img_cache) > _IMG_CACHE_MAX:
        _img_cache.popitem(last=False)  # 删除最早插入的条目


def _img_redis_key(url):
    return "res_web:img:" + hashlib.md5(url.encode()).hexdigest()


def _img_proxy_url_ok(url):
    """校验 img_proxy 的 url 参数，防 SSRF。

    历史 bug: 旧实现用 `"tmdb.org" in url` 子串匹配，可被绕过：
      - https://evil.com/?x=image.tmdb.org       (子串命中，实际回源 evil.com)
      - https://tmdb.org.evil.com/steal          (域名伪装，实际回源攻击者域名)
      - https://image.tmdb.org@evil.com/x.jpg    (userinfo 混淆)
    正确做法: 解析 URL 后只对 hostname 做后缀比对（且域名边界必须是 '.'），
    并把域名解析到 IP，拒绝私网/环回/保留地址，避免 DNS 重绑定与直连内网。

    返回 (ok, reason)。
    """
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    raw = (url or "").strip()
    if not raw:
        return False, "empty"
    try:
        parts = urlsplit(raw)
    except ValueError:
        app.logger.exception("_img_proxy_url_ok failed")
        return False, "unparsable"
    if parts.scheme != "https":
        return False, "scheme"
    if parts.username or parts.password:
        return False, "userinfo"
    host = (parts.hostname or "").lower()
    if not host:
        return False, "no-host"
    # 后缀比对，且必须落在域名边界上（evil-tmdb.org 通过，tmdb.org.evil.com 拒绝）
    if not any(host == d or host.endswith("." + d) for d in _IMG_ALLOWED_HOSTS):
        return False, "host"
    # 域名解析后校验 IP：拒绝私网/环回/链路本地/保留地址
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        app.logger.exception("_img_proxy_url_ok failed")
        return False, "dns"
    if not infos:
        return False, "dns"
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            app.logger.warning("_img_proxy_url_ok failed")
            return False, "bad-ip"
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False, "private-ip"
    return True, ""


DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "172.23.0.2"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD"),
    "database": os.environ.get("DB_NAME", "pan_resource"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "connect_timeout": 10,
    "read_timeout": 30,
    "write_timeout": 30,
}


def get_db():
    return _pool.connection()


def _get_tmdb_api_key():
    """从 settings 表中获取 TMDB API Key，环境变量 TMDB_API_KEY 优先"""
    env_key = os.environ.get("TMDB_API_KEY")
    if env_key:
        return env_key
    try:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT `value` FROM settings WHERE `key`='tmdb_api_key'")
            row = cur.fetchone()
            if row and row.get("value"):
                return row["value"]
        finally:
            db.close()
    except Exception:
        app.logger.exception("_get_tmdb_api_key failed")
        pass
    raise RuntimeError("TMDB API Key 未配置：请设置环境变量 TMDB_API_KEY 或在 settings 表中添加 tmdb_api_key")


def check_rate_limit(key):
    """通用限流。返回 True 表示放行。优先 Redis(跨 worker 共享)，失败降级内存。"""
    try:
        rkey = "res_web:rl:" + hashlib.md5(str(key).encode()).hexdigest()
        r = get_redis()
        cnt = r.incr(rkey)
        if cnt == 1:
            r.expire(rkey, RATE_LIMIT_WINDOW)
        return cnt <= RATE_LIMIT_MAX
    except Exception as e:
        app.logger.warning("限流降级到内存 (Redis 不可用): %s", type(e).__name__)

    now = time.time()
    with _rate_limits_lock:
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


def check_login_limit(ip):
    """检查IP是否被登录限流锁定。返回 (allowed, remaining_seconds)"""
    try:
        r = get_redis()
        k = _login_key(ip)
        cnt = r.get(k)
        if cnt and int(cnt) >= LOGIN_FAIL_MAX:
            ttl = r.ttl(k)
            return False, max(int(ttl), 0) if ttl and ttl > 0 else 0
        return True, 0
    except Exception as e:
        app.logger.warning("登录限流降级到内存 (Redis 不可用): %s", type(e).__name__)

    now = time.time()
    with _login_failures_lock:
        if ip in _login_failures:
            count, last_time = _login_failures[ip]
            if count >= LOGIN_FAIL_MAX:
                elapsed = now - last_time
                if elapsed < LOGIN_LOCK_SECONDS:
                    return False, int(LOGIN_LOCK_SECONDS - elapsed)
                _login_failures.pop(ip, None)
                return True, 0
        return True, 0


def record_login_failure(ip):
    """记录一次登录失败"""
    try:
        r = get_redis()
        k = _login_key(ip)
        cnt = r.incr(k)
        # 每次失败都刷新锁定窗口，持续攻击会一直被锁住
        r.expire(k, LOGIN_LOCK_SECONDS)
        return
    except Exception:
        app.logger.exception("record_login_failure failed")
        pass

    now = time.time()
    with _login_failures_lock:
        if ip in _login_failures:
            count, _ = _login_failures[ip]
            _login_failures[ip] = [count + 1, now]
        else:
            _login_failures[ip] = [1, now]


def reset_login_failures(ip):
    """登录成功后重置失败计数"""
    try:
        get_redis().delete(_login_key(ip))
    except Exception:
        app.logger.exception("reset_login_failures failed")
        pass
    with _login_failures_lock:
        _login_failures.pop(ip, None)


_cache = {}  # key -> (data, expire_time)


def redis_cached(ttl=300, key_prefix="api"):
    """Redis 缓存装饰器：跨 worker 共享缓存，解决进程内 _cache 命中率低的问题。

    - key = res_web:{key_prefix}:{md5(request.url)}
    - 命中返回缓存的 JSON；miss 则执行原函数并回写
    - Redis 不可用时自动降级为直接计算（不缓存），与原有逻辑一致
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = _make_cache_key(key_prefix, request.url)
            try:
                r = get_redis()
                cached = r.get(key)
                if cached:
                    return jsonify(json.loads(cached))
            except Exception:
                app.logger.exception("wrapper failed")
                pass  # Redis 不可用 → 直接计算
            result = f(*args, **kwargs)
            try:
                data = result.get_json()
                if data is not None:
                    r = get_redis()
                    r.setex(key, ttl, json.dumps(data, default=_json_default))
            except Exception:
                app.logger.exception("wrapper failed")
                pass  # 写入失败不影响响应
            return result
        wrapper.__name__ = f.__name__
        wrapper.__doc__ = f.__doc__
        return wrapper
    return decorator


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "未登录，请先登录后台"}), 401
            return redirect("/login")
        return f(*args, **kwargs)

    return decorated


def get_client_ip():
    """统一客户端 IP 获取（P0-2 2026-10-16）。

    站点经 Cloudflare Tunnel 暴露公网时 request.remote_addr 恒为边缘节点 IP，
    登录限流会对所有访客共享计数（误伤正常用户 / 可被利用触发全站登录锁定 DoS）。
    CF-Connecting-IP 由 Cloudflare 写入、不可伪造；直连/内网场景回退 remote_addr。
    """
    cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip
    return request.remote_addr or "unknown"


def _norm_role(role) -> str:
    r = str(role or "").strip().lower()
    if r in ("superadmin", "super", "root"):
        return "superadmin"
    if r == "admin":
        return "admin"
    return "user"


def _is_superadmin_role(role) -> bool:
    return _norm_role(role) == "superadmin"


def _is_staff_role(role) -> bool:
    """能进后台：管理员 + 超管"""
    return _norm_role(role) in ("admin", "superadmin")


def _grant_session(user, touch_login=True):
    role = _norm_role(user.get("role"))
    session["user_id"] = user["id"]
    session["username"] = user.get("username")
    session["role"] = role
    session["is_admin"] = _is_staff_role(role)
    session["is_superadmin"] = _is_superadmin_role(role)
    session.permanent = True
    session.modified = True
    if touch_login:
        try:
            db2 = get_db()
            try:
                db2.cursor().execute("UPDATE users SET last_login=NOW() WHERE id=%s", (user["id"],))
                db2.commit()
            finally:
                db2.close()
        except Exception:
            app.logger.exception("_grant_session failed")
            pass


def _admin_peer_guard(target_uid, target_role):
    """普通管理员不能动管理员/超管；超管可管管理员，不能动其他超管。"""
    my_id = session.get("user_id")
    if target_uid == my_id:
        return "不能对自己执行该操作", 403
    if session.get("is_superadmin"):
        if _is_superadmin_role(target_role):
            return "不能操作其他超管", 403
        return None, None
    if _is_staff_role(target_role):
        return "不能操作其他管理员", 403
    return None, None


def _safe_next_url(raw: str) -> str:
    """只允许站内相对路径，防开放重定向。"""
    u = (raw or "").strip()
    if not u.startswith("/") or u.startswith("//") or "\\" in u:
        return "/"
    if u.startswith("/login"):
        return "/"
    return u[:500]


def _mask_secrets(items, key="password"):
    """未登录访客不应拿到网盘提取码（password）。

    已登录用户保持原样；返回新 list/dict，不改原对象。
    REQUIRE_LOGIN=0 时全站读取面公开，提取码必须对访客屏蔽。
    """
    if items is None:
        return items
    single = isinstance(items, dict)
    seq = [items] if single else list(items)
    if not session.get("user_id"):
        masked = []
        for it in seq:
            if isinstance(it, dict) and it.get(key):
                it = dict(it)
                it[key] = ""
            masked.append(it)
        seq = masked
    return seq[0] if single else seq


GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")


GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")


GITHUB_AUTHORIZATION_BASE_URL = "https://github.com/login/oauth/authorize"


GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"


GITHUB_API_URL = "https://api.github.com/user"


def _oauth_http_proxies():
    """容器直连 github.com 常超时；走本机代理（可用 HTTPS_PROXY / GITHUB_HTTP_PROXY）"""
    proxy = (
        os.environ.get("GITHUB_HTTP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    ).strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _pack_oauth_state(next_url="/", bind=False, uid=None):
    """把 bind/next/uid 打进 state，避免内网开绑 → 公网回调丢 session"""
    return _oauth_serializer().dumps({
        "n": secrets.token_urlsafe(12),
        "next": next_url or "/",
        "bind": bool(bind),
        "uid": uid,
    })


def _unpack_oauth_state(state, max_age=600):
    from itsdangerous import BadSignature, SignatureExpired
    if not state:
        return None
    try:
        return _oauth_serializer().loads(state, max_age=max_age)
    except (BadSignature, SignatureExpired, Exception):
        app.logger.exception("_unpack_oauth_state failed")
        return None


def _build_oauth_redirect_uri():
    """动态构建重定向 URI，优先环境变量，否则用当前请求 Host"""
    env_uri = os.environ.get("GITHUB_REDIRECT_URI", "")
    if env_uri:
        return env_uri
    scheme = request.scheme
    host = request.host
    return f"{scheme}://{host}/login/github/callback"


def _find_or_create_oauth_user(github_id, username, email):
    """通过 github_id 查找用户，找不到则自动创建并返回角色"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, username, role, status FROM users WHERE github_id=%s",
            (str(github_id),),
        )
        user = cur.fetchone()
        if user:
            if user.get("status") != 1:
                return None, "账号已被禁用"
            # 已有 GitHub 账号也必须过群门禁
            ok_gate, gate_msg = _user_pass_internal_beta_gate(user_id=user["id"], role=user.get("role"))
            if not ok_gate:
                return None, gate_msg
            return user, None

        # 开启必加群后，禁止仅用 GitHub 自动注册（须 Telegram 入群登录）
        if _telegram_required_chat():
            return None, "纯内测：请使用 Telegram 登录（需加入指定群组后自动开户）"

        display_name = username or f"github_{github_id}"
        safe_email = (email or "").strip() or None
        cur.execute(
            "INSERT INTO users (username, email, role, status, github_id) VALUES (%s, %s, %s, %s, %s)",
            (display_name, safe_email, "user", 1, str(github_id)),
        )
        db.commit()
        cur.execute(
            "SELECT id, username, role, status FROM users WHERE github_id=%s",
            (str(github_id),),
        )
        new_user = cur.fetchone()
        return new_user, None
    finally:
        db.close()


def _setting_db(key, default=None):
    """只读 settings 表（不读环境变量）"""
    try:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT `value` FROM settings WHERE `key`=%s", (key.lower(),))
            row = cur.fetchone()
            if row and row.get("value") is not None and str(row.get("value")).strip() != "":
                return row["value"]
        finally:
            db.close()
    except Exception:
        app.logger.exception("_setting_db failed")
        pass
    return default


def _telegram_bot_token():
    """优先后台 settings，其次环境变量"""
    v = (_setting_db("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    return v


def _telegram_bot_username():
    """网站登录 Bot，固定 vaultdrive_bot；搜片引流用 _telegram_search_bot。"""
    v = (_setting_db("telegram_bot_username") or os.environ.get("TELEGRAM_BOT_USERNAME") or "vaultdrive_bot").strip().lstrip("@")
    return v or "vaultdrive_bot"


def _telegram_superadmin_ids():
    ids = set(_DEFAULT_TG_SUPERADMINS)
    extra = (_setting_db("telegram_superadmin_ids") or os.environ.get("TELEGRAM_SUPERADMIN_IDS") or "").strip()
    if extra:
        for part in re.split(r"[\s,;]+", extra):
            if part:
                ids.add(part.strip())
    return ids


def _mask_secret(val: str) -> str:
    if not val:
        return ""
    s = str(val)
    if len(s) <= 10:
        return "*" * len(s)
    return s[:6] + "…" + s[-4:]


def _ensure_telegram_id_column():
    """幂等：确保 users.telegram_id 存在"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SHOW COLUMNS FROM users LIKE 'telegram_id'")
        if not cur.fetchone():
            cur.execute(
                "ALTER TABLE users ADD COLUMN telegram_id VARCHAR(64) NULL UNIQUE AFTER github_id"
            )
            db.commit()
    except Exception as e:
        app.logger.warning("ensure telegram_id column: %s", e)
    finally:
        db.close()


def _ensure_telegram_settings_keys():
    """确保后台可编辑的 TG 登录相关 settings 键存在"""
    defaults = [
        ("telegram_bot_token", "", "Telegram Login 用 Bot API Token"),
        ("telegram_bot_username", "vaultdrive_bot", "网站登录用 Bot 用户名（不含@）；搜片引流另见 telegram_search_bot"),
        ("telegram_superadmin_ids", "1562902842", "Telegram 超管 ID，逗号分隔"),
        ("telegram_required_chat", "@heikuangchangshare", "仅该群成员可登录/注册（@群用户名或 -100 数字 ID；空=不限制）"),
        ("telegram_search_bot", "tg38haokuangchang_bot", "搜片 Bot 用户名（引流展示，开发中）"),
        ("telegram_channel", "@heikuangchang", "官方频道"),
    ]
    db = get_db()
    try:
        cur = db.cursor()
        for k, v, desc in defaults:
            cur.execute("SELECT `key` FROM settings WHERE `key`=%s", (k,))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO settings (`key`, value, description) VALUES (%s, %s, %s)",
                    (k, v, desc),
                )
        db.commit()
    except Exception as e:
        app.logger.warning("ensure telegram settings: %s", e)
    finally:
        db.close()


def _telegram_required_chat():
    """必加群：settings > env > 默认矿场群；off/0/false/none/- 关闭"""
    try:
        v = (_setting_db("telegram_required_chat") or os.environ.get("TELEGRAM_REQUIRED_CHAT") or "@heikuangchangshare").strip()
    except Exception:
        app.logger.exception("_telegram_required_chat failed")
        v = (os.environ.get("TELEGRAM_REQUIRED_CHAT") or "@heikuangchangshare").strip()
    if v.lower() in ("off", "0", "false", "none", "-", "disable", "disabled"):
        return ""
    if "t.me/" in v:
        v = v.rstrip("/").split("/")[-1]
    if v and not v.startswith("@") and not v.lstrip("-").isdigit():
        v = "@" + v
    return v


def _is_foreign_ip(client_ip: str) -> bool:
    """精准判断客户端是否为境外 IP。国内 IP / 内网 IP 均返回 False（隐藏 TG 登录）。"""
    import ipaddress
    if not client_ip:
        return False
    try:
        ip_obj = ipaddress.ip_address(client_ip.strip())
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved:
            return False
    except Exception:
        app.logger.exception("_is_foreign_ip failed")
        return False

    r = get_redis()
    cache_key = f"res_web:ip_geo:{client_ip}"
    if r:
        try:
            cached = r.get(cache_key)
            if cached:
                return cached.decode("utf-8") == "foreign"
        except Exception:
            app.logger.exception("_is_foreign_ip failed")
            pass

    # 双通道地理位置解析
    is_foreign = False
    try:
        import urllib.request
        # 1. 优先查国内权威库 pconline，国内 IP 100% 极速命中
        req = urllib.request.Request(
            f"http://whois.pconline.com.cn/ipJson.jsp?ip={client_ip}&json=true",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            data = json.loads(resp.read().decode("gbk", errors="ignore"))
            if data.get("proCode") and data.get("proCode") != "999999":
                is_foreign = False
            else:
                # 2. 非国内库省份时，查 ip-api
                req2 = urllib.request.Request(
                    f"http://ip-api.com/json/{client_ip}?fields=countryCode",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req2, timeout=1.2) as resp2:
                    d2 = json.loads(resp2.read().decode("utf-8"))
                    is_foreign = bool(d2.get("countryCode") and d2.get("countryCode") != "CN")
    except Exception:
        app.logger.exception("_is_foreign_ip failed")
        is_foreign = False

    if r:
        try:
            r.setex(cache_key, 86400, "foreign" if is_foreign else "china")
        except Exception:
            app.logger.exception("_is_foreign_ip failed")
            pass

    return is_foreign


def _growth_info() -> dict:
    """首页/登录/收藏拦截共用的引流文案。"""
    chat = _telegram_required_chat() or "@heikuangchangshare"
    bot = _telegram_search_bot()
    channel = _telegram_channel()
    site = _public_site_url() or "https://vault.aaaaaaaa.host"
    if not site.endswith("/"):
        site = site + "/"
    group_label = chat if (chat.startswith("@") or chat.startswith("http")) else f"@{chat}"
    return {
        "title": "黑矿场资源内测：先进群",
        "group": group_label,
        "group_link": _tme_url(chat),
        "channel": channel if channel.startswith("@") else f"@{channel}",
        "channel_link": _tme_url(channel),
        "search_bot": f"@{bot}",
        "search_bot_link": _tme_url(bot),
        "search_bot_note": "发片名搜片（开发中）",
        "site": site,
        "fav_need_group": True,
        "copy": (
            "黑矿场资源内测：先进群，\n"
            f"私聊 @{bot} 发片名搜片（开发中）。\n"
            f"网站 {site}\n"
            f"频道 {channel if channel.startswith('@') else '@' + channel.lstrip('@')}"
        ),
    }


def group_required_for_favorites(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        blocked = _favorites_gate_response()
        if blocked is not None:
            return blocked
        return f(*args, **kwargs)
    return decorated


def _record_group_chat_activity(chat: dict, from_user: dict):
    """记录必加群内发言（按人/天去重），用于 7 日活跃。"""
    if not from_user or from_user.get("is_bot"):
        return
    if not _is_required_group_chat(chat):
        return
    tg_id = str(from_user.get("id") or "").strip()
    if not tg_id:
        return
    db = get_db()
    try:
        cur = db.cursor()
        _ensure_growth_tables(cur)
        cur.execute(
            "INSERT IGNORE INTO group_activity (tg_id, day) VALUES (%s, CURDATE())",
            (tg_id,),
        )
        db.commit()
    except Exception as e:
        app.logger.debug("group_activity: %s", e)
    finally:
        db.close()


def _growth_metrics():
    """引流监测：群人数、7 日群活跃、7 日登录、7 日搜索。"""
    goal = GROWTH_MEMBER_GOAL
    members, member_err = _telegram_member_count()
    db = get_db()
    try:
        cur = db.cursor()
        _ensure_growth_tables(cur)
        db.commit()
        cur.execute(
            "SELECT COUNT(*) c FROM group_activity WHERE day >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)"
        )
        chatters_7d = (cur.fetchone() or {}).get("c") or 0
        cur.execute(
            """SELECT day, COUNT(*) c FROM group_activity
               WHERE day >= DATE_SUB(CURDATE(), INTERVAL 13 DAY)
               GROUP BY day ORDER BY day"""
        )
        activity_daily = []
        for row in cur.fetchall() or []:
            activity_daily.append({
                "date": str(row.get("day") or "")[:10],
                "count": row.get("c") or 0,
            })
        cur.execute(
            "SELECT COUNT(*) c FROM users WHERE last_login >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
        )
        logins_7d = (cur.fetchone() or {}).get("c") or 0
        cur.execute(
            "SELECT COUNT(*) c FROM users WHERE last_login >= CURDATE()"
        )
        logins_today = (cur.fetchone() or {}).get("c") or 0
        cur.execute(
            "SELECT COUNT(*) c, COUNT(DISTINCT user_id) u FROM search_logs WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
        )
        row = cur.fetchone() or {}
        searches_7d = row.get("c") or 0
        searchers_7d = row.get("u") or 0
        cur.execute(
            "SELECT COUNT(*) c FROM search_logs WHERE created_at >= CURDATE()"
        )
        searches_today = (cur.fetchone() or {}).get("c") or 0
        cur.execute(
            """SELECT DATE(counted_at) d, MAX(member_count) member_count
               FROM group_member_snapshots
               WHERE counted_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
               GROUP BY DATE(counted_at)
               ORDER BY d"""
        )
        snaps = []
        for s in cur.fetchall() or []:
            snaps.append({
                "date": str(s.get("d") or "")[:10],
                "count": s.get("member_count") or 0,
            })
    finally:
        db.close()
    progress = 0.0
    if members:
        progress = min(1.0, members / float(goal))
    watery = bool(members and members >= 8000 and chatters_7d < 300)
    return {
        "goal": goal,
        "chat": _telegram_required_chat(),
        "members": members,
        "members_error": member_err,
        "progress": round(progress, 4),
        "ready_to_monetize": False,  # 现阶段只引流，暂不变现
        "watery": watery,
        "group_chatters_7d": int(chatters_7d),
        "group_activity_daily": activity_daily,
        "site_logins_7d": int(logins_7d),
        "site_logins_today": int(logins_today),
        "searches_7d": int(searches_7d),
        "searchers_7d": int(searchers_7d),
        "searches_today": int(searches_today),
        "member_history": snaps[-30:],
    }


def _telegram_group_gate(telegram_id) -> tuple[bool, str]:
    """统一门禁文案"""
    ok, detail = _telegram_user_in_required_group(telegram_id)
    if ok:
        return True, ""
    chat = _telegram_required_chat() or "@heikuangchangshare"
    link = chat if chat.startswith("http") else f"https://t.me/{chat.lstrip('@')}"
    return False, f"纯内测：仅限群组成员。请先加入 {link} 后再试（当前状态: {detail}）"


def _user_pass_internal_beta_gate(user_id=None, telegram_id=None, role=None) -> tuple[bool, str]:
    """纯内测总闸：开启必加群后，已绑定 Telegram 的账号需在群内；普通邮箱/站内账号允许正常登录。"""
    if not _telegram_required_chat():
        return True, ""
    if _is_staff_role(role) or _is_superadmin_role(role):
        return True, ""
    tg_id = str(telegram_id or "").strip() or None
    has_email_or_local = False
    if user_id:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT telegram_id, role, email, username FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone() or {}
            if not tg_id:
                tg_id = row.get("telegram_id")
            if role is None:
                role = row.get("role")
            if row.get("email") or row.get("username"):
                has_email_or_local = True
        finally:
            db.close()
    if not tg_id:
        if has_email_or_local:
            return True, ""
        chat = _telegram_required_chat()
        link = chat if chat.startswith("http") else f"https://t.me/{chat.lstrip('@')}"
        return False, f"纯内测：请使用 Telegram 登录，并先加入 {link}"
    return _telegram_group_gate(tg_id)


def _verify_telegram_login(data: dict) -> tuple[bool, str]:
    """校验 Telegram Login Widget 返回的 hash。
    文档: https://core.telegram.org/widgets/login#checking-authorization
    """
    token = _telegram_bot_token()
    if not token:
        return False, "服务端未配置 Telegram Bot Token（请在后台系统设置添加）"
    recv_hash = (data.get("hash") or "").strip()
    if not recv_hash:
        return False, "缺少 hash"
    try:
        auth_date = int(data.get("auth_date") or 0)
    except (TypeError, ValueError):
        app.logger.exception("_verify_telegram_login failed")
        return False, "auth_date 无效"
    if not auth_date:
        return False, "缺少 auth_date"
    if TELEGRAM_AUTH_MAX_AGE > 0 and (time.time() - auth_date) > TELEGRAM_AUTH_MAX_AGE:
        return False, "登录信息已过期，请重试"

    check_fields = []
    for key in sorted(k for k in data.keys() if k != "hash"):
        val = data.get(key)
        if val is None or val == "":
            continue
        check_fields.append(f"{key}={val}")
    data_check_string = "\n".join(check_fields)
    secret_key = hashlib.sha256(token.encode("utf-8")).digest()
    import hmac as _hmac

    calc = _hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(calc, recv_hash):
        return False, "Telegram 签名校验失败"
    return True, ""


def _promote_telegram_superadmin(cur, user, telegram_id):
    """超管 TG ID 强制 role=superadmin"""
    if str(telegram_id) not in _telegram_superadmin_ids():
        return user
    if _norm_role(user.get("role")) != "superadmin":
        cur.execute("UPDATE users SET role='superadmin' WHERE id=%s", (user["id"],))
        user = dict(user)
        user["role"] = "superadmin"
    return user


def _find_or_create_telegram_user(telegram_id, username, first_name, last_name):
    """通过 telegram_id 查找或自动创建用户；超管名单自动升为 admin；需通过必加群门禁"""
    tg_id = str(telegram_id)
    ok_gate, gate_msg = _telegram_group_gate(tg_id)
    if not ok_gate:
        return None, gate_msg
    is_super = tg_id in _telegram_superadmin_ids()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, username, role, status FROM users WHERE telegram_id=%s",
            (tg_id,),
        )
        user = cur.fetchone()
        if not user:
            # 兼容：老账号 username 直接等于 TG 数字 ID
            cur.execute(
                "SELECT id, username, role, status, telegram_id FROM users WHERE username=%s",
                (tg_id,),
            )
            legacy = cur.fetchone()
            if legacy:
                if not legacy.get("telegram_id"):
                    cur.execute(
                        "UPDATE users SET telegram_id=%s WHERE id=%s",
                        (tg_id, legacy["id"]),
                    )
                user = {
                    "id": legacy["id"],
                    "username": legacy["username"],
                    "role": legacy["role"],
                    "status": legacy["status"],
                }

        if user:
            if user.get("status") != 1:
                return None, "账号已被禁用"
            user = _promote_telegram_superadmin(cur, user, tg_id)
            db.commit()
            return user, None

        display = (username or "").strip()
        if not display:
            parts = [p for p in [(first_name or "").strip(), (last_name or "").strip()] if p]
            display = " ".join(parts) or f"tg_{tg_id}"
        base = display[:80]
        candidate = base
        n = 1
        while True:
            cur.execute("SELECT id FROM users WHERE username=%s", (candidate,))
            if not cur.fetchone():
                break
            n += 1
            candidate = f"{base}_{n}"[:100]

        role = "superadmin" if is_super else "user"
        cur.execute(
            """INSERT INTO users (username, email, role, status, telegram_id, points, created_at, updated_at) 
            VALUES (%s, %s, %s, 1, %s, 100, NOW(), NOW())""",
            (candidate, None, role, tg_id),
        )
        new_uid = cur.lastrowid
        cur.execute(
            """INSERT INTO point_logs (user_id, action, change_amount, balance_after, remark, created_at) 
            VALUES (%s, 'register', 100, 100, 'Telegram 注册赠送初始积分', NOW())""",
            (new_uid,),
        )
        db.commit()
        cur.execute(
            "SELECT id, username, role, status FROM users WHERE telegram_id=%s",
            (tg_id,),
        )
        return cur.fetchone(), None
    except pymysql.err.IntegrityError as e:
        app.logger.exception("_find_or_create_telegram_user failed")
        return None, f"创建用户失败: {e}"
    finally:
        db.close()


def _session_login_user(user):
    _grant_session(user, touch_login=True)


TG_BIND_TTL = 600  # 绑定码 10 分钟


TG_LOGIN_TTL = 600


def _telegram_webhook_secret():
    raw = (os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    if raw:
        return raw
    # 稳定派生，避免明文 token 进 URL
    material = f"{app.secret_key or 'vault'}:tg-webhook"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _telegram_api(method, payload=None, timeout=20):
    """调用 Bot API；优先直连，失败再走代理（api.telegram.org 在部分网络不稳定）"""
    token = _telegram_bot_token()
    if not token:
        raise RuntimeError("未配置 TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/{method}"
    proxies_list = [None]
    p = _oauth_http_proxies()
    if p:
        proxies_list.append(p)
    last_err = None
    for proxies in proxies_list:
        for attempt in range(2):
            try:
                r = http_requests.post(
                    url,
                    json=payload or {},
                    timeout=timeout,
                    proxies=proxies,
                )
                data = r.json() if r.content else {}
                if not data.get("ok"):
                    raise RuntimeError(data.get("description") or f"Telegram API {method} failed: {r.status_code}")
                return data.get("result")
            except Exception as e:
                app.logger.warning("_telegram_api failed: %s", e)
                last_err = e
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(str(last_err) if last_err else f"Telegram API {method} failed")


def _telegram_webhook_base():
    """公网基址：优先 TELEGRAM_WEBHOOK_BASE，否则从 GITHUB_REDIRECT_URI 推断"""
    base = (os.environ.get("TELEGRAM_WEBHOOK_BASE") or "").strip().rstrip("/")
    if base:
        return base
    redir = (os.environ.get("GITHUB_REDIRECT_URI") or "").strip()
    if redir:
        from urllib.parse import urlparse
        p = urlparse(redir)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    return ""


def _ensure_telegram_webhook():
    """幂等设置 webhook（Redis 标记，避免每次请求打 TG）"""
    base = _telegram_webhook_base()
    if not base or not _telegram_bot_token():
        return False, "未配置公网域名或 Bot Token"
    r = get_redis()
    cache_key = "tg_webhook:ready:v3"
    if r is not None:
        try:
            cached = r.get(cache_key)
            if isinstance(cached, bytes):
                cached = cached.decode()
            if cached == base:
                return True, "cached"
        except Exception:
            app.logger.exception("_ensure_telegram_webhook failed")
            pass
    secret = _telegram_webhook_secret()
    hook = f"{base}/api/telegram/bot-webhook/{secret}"
    try:
        _telegram_api("setWebhook", {
            "url": hook,
            "allowed_updates": ["message", "inline_query"],
            "drop_pending_updates": False,
        })
        try:
            _telegram_api("setMyCommands", {
                "commands": [
                    {"command": "s", "description": "搜索资源，例：/s 流浪地球"},
                    {"command": "search", "description": "搜索资源"},
                    {"command": "help", "description": "使用说明"},
                ]
            })
        except Exception as e:
            app.logger.debug("setMyCommands: %s", e)
        if r is not None:
            try:
                r.delete("tg_webhook:ready")
                r.setex(cache_key, 86400, base)
            except Exception:
                app.logger.exception("_ensure_telegram_webhook failed")
                pass
        app.logger.info("Telegram webhook set: %s", hook)
        return True, hook
    except Exception as e:
        app.logger.warning("setWebhook failed: %s", e)
        return False, str(e)


def _tg_help_text():
    """对外引流文案：搜片 Bot 固定为 @tg38haokuangchang_bot。"""
    return _growth_info()["copy"]


def _tg_handle_search(from_user, reply_fn, keyword):
    tg_id = from_user.get("id") if from_user else None
    if not tg_id:
        reply_fn("无法读取你的 Telegram ID。")
        return
    ok_gate, gate_msg = _telegram_group_gate(tg_id)
    if not ok_gate:
        reply_fn(gate_msg)
        return
    kw = (keyword or "").strip()
    if len(kw) < 2:
        reply_fn("用法：直接发片名，或 /s 流浪地球")
        return
    if not _tg_search_rate_ok(tg_id):
        reply_fn("搜得太快了，稍等一会儿再试。")
        return
    try:
        items, display = _bot_search_resources(kw, limit=8)
    except Exception as e:
        app.logger.warning("tg search failed: %s", e)
        reply_fn("搜索暂时不可用，请稍后再试或打开网站。")
        return
    _tg_log_search(display or kw, tg_id)
    reply_fn(_format_tg_search_results(items, display or kw))


def _tg_handle_inline_query(iq: dict):
    qid = iq.get("id")
    if not qid:
        return
    from_user = iq.get("from") or {}
    tg_id = from_user.get("id")
    query = (iq.get("query") or "").strip()
    payload = {
        "inline_query_id": qid,
        "results": [],
        "cache_time": 10,
        "is_personal": True,
    }
    if not tg_id:
        _telegram_api("answerInlineQuery", payload)
        return
    ok_gate, _gate = _telegram_group_gate(tg_id)
    if not ok_gate:
        payload["switch_pm_text"] = "请先加群再搜索"
        payload["switch_pm_parameter"] = "join"
        payload["cache_time"] = 5
        _telegram_api("answerInlineQuery", payload)
        return
    if len(query) < 2:
        bot = _telegram_bot_username() or "vaultdrive_bot"
        payload["switch_pm_text"] = "私聊发片名搜索"
        payload["switch_pm_parameter"] = "search"
        _telegram_api("answerInlineQuery", payload)
        return
    if not _tg_search_rate_ok(tg_id):
        _telegram_api("answerInlineQuery", payload)
        return
    items, display = _bot_search_resources(query, limit=10)
    _tg_log_search(display or query, tg_id)
    results = []
    for it in items:
        title = (it.get("title") or it.get("keyword") or "资源").strip()[:64]
        meta = " · ".join(
            str(x) for x in (it.get("year"), it.get("source"), it.get("quality")) if x
        )
        url = (it.get("url") or "").strip()
        pwd = (it.get("password") or "").strip()
        body = title
        if meta:
            body += f"\n{meta}"
        if url:
            body += f"\n{url}"
        if pwd:
            body += f"\n提取码: {pwd}"
        results.append({
            "type": "article",
            "id": str(it.get("id"))[:64],
            "title": title,
            "description": (meta or url or "打开查看链接")[:120],
            "input_message_content": {
                "message_text": body[:4096],
                "disable_web_page_preview": False,
            },
        })
    payload["results"] = results
    payload["cache_time"] = 20
    _telegram_api("answerInlineQuery", payload)


def _tg_bind_store(code, user_id):
    r = get_redis()
    if r is None:
        raise RuntimeError("Redis 不可用，无法生成绑定码")
    r.setex(TG_BIND_PREFIX + code, TG_BIND_TTL, str(user_id))


def _tg_bind_pop(code):
    r = get_redis()
    if r is None:
        return None
    key = TG_BIND_PREFIX + code
    uid = r.get(key)
    if uid is not None:
        r.delete(key)
        if isinstance(uid, bytes):
            uid = uid.decode()
        try:
            return int(uid)
        except (TypeError, ValueError):
            app.logger.exception("_tg_bind_pop failed")
            return None
    return None


def _bind_telegram_to_user(user_id, telegram_id):
    """把 telegram_id 绑到指定用户；冲突返回 (False, msg)"""
    _ensure_telegram_id_column()
    tg_id = str(telegram_id)
    ok_gate, gate_msg = _telegram_group_gate(tg_id)
    if not ok_gate:
        return False, gate_msg
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id, username, role, status, telegram_id FROM users WHERE id=%s", (user_id,))
        user = cur.fetchone()
        if not user:
            return False, "用户不存在"
        if user.get("status") != 1:
            return False, "账号已被禁用"
        if user.get("telegram_id") and str(user.get("telegram_id")) != tg_id:
            return False, "该账号已绑定其他 Telegram"
        if user.get("telegram_id") and str(user.get("telegram_id")) == tg_id:
            return True, "already"
        cur.execute(
            "SELECT id, username FROM users WHERE telegram_id=%s AND id<>%s",
            (tg_id, user_id),
        )
        taken = cur.fetchone()
        if taken:
            return False, "该 Telegram 已绑定其他账号"
        cur.execute("UPDATE users SET telegram_id=%s WHERE id=%s", (tg_id, user_id))
        user = _promote_telegram_superadmin(cur, user, tg_id)
        db.commit()
        return True, "bound"
    finally:
        db.close()


def _tg_login_store_pending(code):
    r = get_redis()
    if r is None:
        raise RuntimeError("Redis 不可用")
    r.setex(TG_LOGIN_PREFIX + code, TG_LOGIN_TTL, json.dumps({"status": "pending"}))


def _tg_login_mark_ready(code, tg_user):
    r = get_redis()
    if r is None:
        return False
    key = TG_LOGIN_PREFIX + code
    if not r.exists(key):
        return False
    r.setex(key, TG_LOGIN_TTL, json.dumps({
        "status": "ready",
        "tg_id": str(tg_user.get("id") or ""),
        "username": tg_user.get("username") or "",
        "first_name": tg_user.get("first_name") or "",
        "last_name": tg_user.get("last_name") or "",
    }, ensure_ascii=False))
    return True


def _tg_login_get(code):
    r = get_redis()
    if r is None:
        return None
    raw = r.get(TG_LOGIN_PREFIX + code)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except Exception:
        app.logger.exception("_tg_login_get failed")
        return None


def _tg_login_consume(code):
    r = get_redis()
    if r is None:
        return None
    key = TG_LOGIN_PREFIX + code
    raw = r.get(key)
    if not raw:
        return None
    r.delete(key)
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except Exception:
        app.logger.exception("_tg_login_consume failed")
        return None


def _get_visit_counts():
    """读取累计/今日访问；Redis 优先，settings 兜底并回填 Redis。"""
    total, today = 0, 0
    daily_key = _visit_today_key()
    try:
        r = get_redis()
        raw = r.get(_VISIT_REDIS_KEY)
        if raw is not None:
            total = int(raw)
        else:
            total = int(_setting_db("total_visits", "0") or 0)
            r.set(_VISIT_REDIS_KEY, total)
        raw_d = r.get(daily_key)
        today = int(raw_d) if raw_d is not None else 0
    except Exception:
        app.logger.exception("_get_visit_counts failed")
        try:
            total = int(_setting_db("total_visits", "0") or 0)
        except (TypeError, ValueError):
            app.logger.exception("_get_visit_counts failed")
            total = 0
        today = 0
    return total, today


def _incr_visit_counts():
    """原子自增累计/今日访问，并持久化到 settings。"""
    daily_key = _visit_today_key()
    try:
        r = get_redis()
        # 首次从 DB 回填，避免 Redis 空 key 从 0 重新计
        if r.get(_VISIT_REDIS_KEY) is None:
            seed = int(_setting_db("total_visits", "0") or 0)
            r.set(_VISIT_REDIS_KEY, seed)
        total = int(r.incr(_VISIT_REDIS_KEY))
        today = int(r.incr(daily_key))
        r.expire(daily_key, 86400 * 3)
        if total % 10 == 0:
            _upsert_setting("total_visits", str(total), "累计访问量")
        return total, today
    except Exception:
        # Redis 不可用时走 MySQL 原子更新
        app.logger.exception("_incr_visit_counts failed")
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO settings (`key`, value, description) VALUES ('total_visits', '1', '累计访问量') "
                "ON DUPLICATE KEY UPDATE value = CAST(value AS UNSIGNED) + 1"
            )
            db.commit()
            cur.execute("SELECT value FROM settings WHERE `key`='total_visits'")
            row = cur.fetchone()
            total = int((row or {}).get("value") or 1)
            return total, 0
        finally:
            db.close()


def _merge_source(src):
    """把 plugin:xxx 归类到主网盘类型"""
    if src.startswith("plugin:"):
        return "other"
    return src


def _get_cached_filter_words():
    """从内存缓存获取filter_words列表，TTL 300秒"""
    now = time.time()
    if _FW_CACHE_KEY in _cache:
        data, expire = _cache[_FW_CACHE_KEY]
        if now < expire:
            return data
    # 缓存过期，从DB加载
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT word FROM filter_words")
        fwords = [r["word"] for r in cur.fetchall()]
    finally:
        db.close()
    _cache[_FW_CACHE_KEY] = (fwords, now + _FW_CACHE_TTL)
    return fwords


def _clear_filter_words_cache():
    """清除filter_words缓存"""
    _cache.pop(_FW_CACHE_KEY, None)
    _invalidate_resource_cache()


def _filter_words_condition(cur):
    """
    v2: 结构化优化，放弃 70+ NOT LIKE 全表扫。
    直接利用 DB 层新增的 is_filtered = 0 字段查询。
    """
    return "is_filtered = 0", []


def _get_import_api_urls():
    """从 settings 表读取 import_api_url（支持多个，换行分隔），失败回退默认"""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT value FROM settings WHERE `key`='import_api_url'")
        row = cur.fetchone()
        db.close()
        if row and row["value"]:
            urls = [u.strip() for u in row["value"].split("\n") if u.strip()]
            if urls:
                return urls
    except Exception:
        app.logger.exception("_get_import_api_urls failed")
        pass
    return ["https://pansou.42078207.qzz.io/api/search", "https://pansou.42078207.xyz/api/search"]


def _should_run_online_search(keyword, cooldown_seconds=600):
    """v20260807_152427: 是否应触发在线搜索？

    去重逻辑：
    - Redis 中 status='running' → 正在搜，跳过（避免重复启动）
    - Redis 中 status='done' 且在 cooldown_seconds 内 → 刚搜过，跳过（避免刷接口）
    - Redis 中状态 'error' 或不存在 → 可以启动

    这样保证同一个 keyword 在 cooldown 时间内只搜一次，但用户能即时看到上次结果。
    """
    try:
        status_key = f"online_search_status:{keyword}"
        cached = get_redis().get(status_key)
        if not cached:
            return True  # 从未搜过
        data = json.loads(cached)
        status = data.get("status", "")
        if status == "running":
            return False  # 正在搜
        if status == "done":
            # 检查是否在冷却时间内
            started_at = data.get("started_at", 0)
            if time.time() - started_at < cooldown_seconds:
                return False  # 刚搜过，跳过
            return True  # 冷却时间过了，可以重新搜
        # error 或其他状态 → 可以搜
        return True
    except Exception:
        app.logger.exception("_should_run_online_search failed")
        return True  # 出错时允许搜（保守）


def _async_online_search(keyword, limit=20):
    """异步在线搜索：后台调用 pansou，结果直接入库（DB 是唯一真实来源）
    Redis 只用于存状态/进度给前端轮询，不存 items 数据本身。
    """
    status_key = f"online_search_status:{keyword}"
    try:
        # 标记开始
        try:
            get_redis().setex(status_key, 300, json.dumps({
                "status": "running", "started_at": time.time(), "imported": 0
            }))
        except Exception:
            app.logger.exception("_async_online_search failed")
            pass

        items = _search_online_sync(keyword, limit=limit)
        if items:
            # 直接入库（DB 是唯一来源）
            imported, skipped_dead = _background_import(keyword, items, return_counts=True)
            # 标记完成
            try:
                get_redis().setex(status_key, 300, json.dumps({
                    "status": "done",
                    "started_at": time.time(),
                    "imported": imported,
                    "skipped_dead": skipped_dead,
                }))
            except Exception:
                app.logger.exception("_async_online_search failed")
                pass
        else:
            try:
                get_redis().setex(status_key, 300, json.dumps({
                    "status": "done", "started_at": time.time(),
                    "imported": 0, "skipped_dead": 0,
                }))
            except Exception:
                app.logger.exception("_async_online_search failed")
                pass
    except Exception as e:
        try:
            get_redis().setex(status_key, 300, json.dumps({
                "status": "error", "started_at": time.time(), "error": str(e)
            }))
        except Exception:
            app.logger.exception("_async_online_search failed")
            pass
        print(f"[async-search] error for '{keyword}': {e}")


def add_user_points(cur, user_id: int, action: str, amount: int, remark: str = "") -> int:
    """原子化变动用户积分并记入流水日志，返回变动后积分"""
    cur.execute("SELECT points FROM users WHERE id=%s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    if not row:
        return 0
    curr = int(row.get("points") or 0)
    new_bal = max(0, curr + amount)
    cur.execute("UPDATE users SET points=%s, updated_at=NOW() WHERE id=%s", (new_bal, user_id))
    cur.execute(
        "INSERT INTO point_logs (user_id, action, change_amount, balance_after, remark, created_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW())",
        (user_id, action, amount, new_bal, remark),
    )
    return new_bal


def _ensure_watchlist_table():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
              id BIGINT NOT NULL AUTO_INCREMENT,
              user_id INT NOT NULL,
              tmdb_id BIGINT NOT NULL,
              media_type VARCHAR(16) NOT NULL DEFAULT 'movie',
              title VARCHAR(255) NOT NULL DEFAULT '',
              poster TEXT NULL,
              year VARCHAR(8) NULL,
              rating VARCHAR(16) NULL,
              overview TEXT NULL,
              created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              UNIQUE KEY uk_user_tmdb (user_id, tmdb_id, media_type),
              KEY idx_watchlist_user (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        db.commit()
    except Exception as e:
        app.logger.warning("ensure watchlist table: %s", e)
    finally:
        db.close()


def _ensure_announcements_table():
    """公告表：历史上漏建 CREATE TABLE，导致首页 /api/announcements 必现 500。

    列名以 app.py 内实际 SELECT/INSERT/UPDATE 用到的为准：
      id, title, content, type, active, start_time, end_time, created_at, updated_at
    幂等（IF NOT EXISTS）；失败只告警不抛，不影响请求。
    """
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS announcements (
              id BIGINT NOT NULL AUTO_INCREMENT,
              title VARCHAR(255) NOT NULL DEFAULT '',
              content MEDIUMTEXT NULL,
              type VARCHAR(32) NOT NULL DEFAULT 'text',
              active TINYINT(1) NOT NULL DEFAULT 1,
              start_time DATETIME NULL DEFAULT NULL,
              end_time DATETIME NULL DEFAULT NULL,
              created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              KEY idx_active_window (active, start_time, end_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        db.commit()
    except Exception as e:
        app.logger.warning("ensure announcements table: %s", e)
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
    except:
        app.logger.exception("_log_op failed")
        pass


def _auto_scan_unchecked_bg(limit=50):
    """并发扫描未检测的资源链接（自动多线程并发）"""
    from concurrent.futures import ThreadPoolExecutor
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """SELECT id, source, url FROM resources 
            WHERE (source NOT IN ('magnet','ed2k','other') AND url NOT LIKE 'magnet:%' AND url NOT LIKE 'ed2k:%') 
              AND last_checked IS NULL 
            ORDER BY id DESC LIMIT %s""", (limit,)
        )
        items = cur.fetchall()
        db.close()

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
                db2 = get_db()
                try:
                    cur2 = db2.cursor()
                    cur2.execute("UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s", (status, rid))
                    db2.commit()
                finally:
                    db2.close()
            except Exception:
                app.logger.exception("_check_and_update failed")
                pass

        with ThreadPoolExecutor(max_workers=15) as pool:
            pool.map(_check_and_update, items)
    except Exception:
        app.logger.exception("_auto_scan_unchecked_bg failed")
        pass


def _check_single_link(url, source=""):
    """委托给 services.link_checker（各盘官方/半官方 API）。"""
    from services.link_checker import check_single_link

    return check_single_link(url, source)


_tmdb_crawler_status = {"running": False, "progress": 0, "total": 0, "imported": 0, "log": []}


_multi_crawler_status = {"running": False, "phase": "", "progress": 0, "total": 0, "imported": 0, "log": []}


_omdb_crawler_status = {"running": False, "progress": 0, "total": 0, "success": 0, "failed": 0, "log": []}


_bg_scan_state = {
    "running": False,
    "total": 0,
    "scanned": 0,
    "alive": 0,
    "dead": 0,
    "start_time": 0,
    "elapsed_s": 0,
    "last_error": "",
    "source_filter": "",
    "rescan_alive": False,
}


_bg_scan_stop_event = threading.Event()


def _bg_scan_worker_thread(source_filter="", batch_size=1000, rescan_alive=False, stale_days=3):
    """进程内兜底巡检（无独立 worker 容器时使用）。"""
    global _bg_scan_state
    _bg_scan_state["running"] = True
    _bg_scan_state["scanned"] = 0
    _bg_scan_state["alive"] = 0
    _bg_scan_state["dead"] = 0
    _bg_scan_state["start_time"] = time.time()
    _bg_scan_state["source_filter"] = source_filter
    _bg_scan_state["rescan_alive"] = rescan_alive
    _bg_scan_stop_event.clear()

    try:
        db = get_db()
        cur = db.cursor()
        exclude_sources = "('magnet', 'ed2k', 'plugin:thepiratebay', 'plugin:nyaa')"
        conds = [f"source NOT IN {exclude_sources} AND url NOT LIKE 'magnet:%' AND url NOT LIKE 'ed2k:%'"]
        params = []
        if source_filter:
            conds.append("source = %s")
            params.append(source_filter)
        if rescan_alive:
            conds.append(
                "(last_checked IS NULL OR link_status IS NULL OR link_status IN ('','unknown','timeout','error','suspect') "
                "OR (link_status='alive' AND (last_checked IS NULL OR last_checked < DATE_SUB(NOW(), INTERVAL %s DAY))))"
            )
            params.append(int(stale_days))
        else:
            conds.append("(last_checked IS NULL OR link_status IS NULL OR link_status = '' OR link_status = 'unknown')")

        where = " AND ".join(conds)
        cur.execute(f"SELECT COUNT(*) as c FROM resources WHERE {where}", params)
        total_links = cur.fetchone()["c"]
        _bg_scan_state["total"] = total_links
        db.close()

        workers = int(os.environ.get("DEADLINK_THREADS", "25"))
        while not _bg_scan_stop_event.is_set():
            db = get_db()
            cur = db.cursor()
            cur.execute(
                f"SELECT id, url, source FROM resources WHERE {where} ORDER BY id DESC LIMIT %s",
                params + [batch_size],
            )
            rows = cur.fetchall()
            db.close()

            if not rows:
                break

            def _check_row(row):
                if _bg_scan_stop_event.is_set():
                    return None
                rid = row["id"]
                url = row["url"]
                src = (row.get("source") or "").lower()
                status, code, msg = _check_single_link(url, src)
                return rid, status

            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(_check_row, rows))

            db = get_db()
            cur = db.cursor()
            for res in results:
                if not res:
                    continue
                rid, status = res
                cur.execute(
                    "UPDATE resources SET link_status=%s, last_checked=NOW() WHERE id=%s",
                    (status, rid),
                )
                _bg_scan_state["scanned"] += 1
                if status == "dead":
                    _bg_scan_state["dead"] += 1
                elif status == "alive":
                    _bg_scan_state["alive"] += 1

            db.commit()
            db.close()
            _bg_scan_state["elapsed_s"] = int(time.time() - _bg_scan_state["start_time"])
            _invalidate_resource_cache()
            time.sleep(0.5)

    except Exception as e:
        app.logger.exception("_bg_scan_worker_thread failed")
        _bg_scan_state["last_error"] = str(e)
    finally:
        _bg_scan_state["running"] = False
        _bg_scan_state["elapsed_s"] = int(time.time() - _bg_scan_state["start_time"])


def _cluster_workers_online() -> int:
    try:
        from services.celery_app import app as celery_app

        insp = celery_app.control.inspect(timeout=1.0)
        pings = insp.ping() if insp else None
        if pings:
            return len(pings)
    except Exception:
        app.logger.exception("_cluster_workers_online failed")
        pass
    try:
        from services.deadlink_queue import get_state

        return int(get_state().get("workers") or 0)
    except Exception:
        app.logger.exception("_cluster_workers_online failed")
        return 0



_redis_client = None


_IMG_CACHE_MAX = 200  # 单进程内存缓存条目数（Redis 才是主缓存，这里只做热点快取）


_IMG_ALLOWED_HOSTS = ("tmdb.org", "doubanio.com")


_pool = PooledDB(
    creator=pymysql,
    maxconnections=30,
    mincached=3,
    maxcached=8,
    blocking=True,
    maxusage=None,
    setsession=["SET NAMES utf8mb4", "SET time_zone = '+08:00'"],
    ping=1,  # ping before use
    **DB_CONFIG,
)


def _get_setting(key, default=None):
    """从 settings 表读取配置，环境变量优先，数据库兜底"""
    env_val = os.environ.get(key.upper())
    if env_val:
        return env_val
    try:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT `value` FROM settings WHERE `key`=%s", (key.lower(),))
            row = cur.fetchone()
            if row and row.get("value"):
                return row["value"]
        finally:
            db.close()
    except Exception:
        pass
    return default


_rate_limits = {}  # 降级兜底: key -> [count, window_start]


_rate_limits_lock = _threading.Lock()


RATE_LIMIT_WINDOW = 60  # seconds


RATE_LIMIT_MAX = 10  # max requests per window


_login_failures = {}  # 降级兜底: ip -> [fail_count, last_fail_time]


_login_failures_lock = _threading.Lock()


LOGIN_FAIL_MAX = 5


LOGIN_LOCK_SECONDS = 900  # 15 minutes


def _login_key(ip):
    return "res_web:loginfail:" + hashlib.md5(str(ip).encode()).hexdigest()


def _oauth_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.secret_key, salt="github-oauth-v1")


TELEGRAM_AUTH_MAX_AGE = int(os.environ.get("TELEGRAM_AUTH_MAX_AGE", "86400"))


_DEFAULT_TG_SUPERADMINS = {"1562902842"}


def _upsert_setting(key, value, description=None):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT `key` FROM settings WHERE `key`=%s", (key,))
        if cur.fetchone():
            cur.execute("UPDATE settings SET value=%s WHERE `key`=%s", (str(value), key))
        else:
            cur.execute(
                "INSERT INTO settings (`key`, value, description) VALUES (%s, %s, %s)",
                (key, str(value), description or ""),
            )
        db.commit()
    finally:
        db.close()


def _tme_url(handle: str) -> str:
    h = (handle or "").strip()
    if not h:
        return ""
    if h.startswith("http://") or h.startswith("https://"):
        return h
    return f"https://t.me/{h.lstrip('@')}"


def _telegram_search_bot() -> str:
    try:
        v = (_setting_db("telegram_search_bot") or os.environ.get("TELEGRAM_SEARCH_BOT") or "tg38haokuangchang_bot").strip()
    except Exception:
        v = (os.environ.get("TELEGRAM_SEARCH_BOT") or "tg38haokuangchang_bot").strip()
    return (v or "tg38haokuangchang_bot").lstrip("@")


def _telegram_channel() -> str:
    try:
        v = (_setting_db("telegram_channel") or os.environ.get("TELEGRAM_CHANNEL") or "@heikuangchang").strip()
    except Exception:
        v = (os.environ.get("TELEGRAM_CHANNEL") or "@heikuangchang").strip()
    if not v:
        v = "@heikuangchang"
    if v.startswith("http") or v.startswith("@"):
        return v
    return "@" + v.lstrip("@")


def _favorites_gate_response():
    """收藏必须登录且在必加群。放行返回 None。"""
    growth = _growth_info()
    uid = session.get("user_id")
    if not uid:
        return jsonify({
            "error": "未进群不能使用收藏，请先加入内测群",
            "join_required": True,
            "login_required": True,
            "growth": growth,
        }), 401
    ok, msg = _user_pass_internal_beta_gate(user_id=uid, role=session.get("role"))
    if not ok:
        return jsonify({
            "error": msg or "未进群不能使用收藏，请先加入内测群",
            "join_required": True,
            "growth": growth,
        }), 403
    return None


GROWTH_MEMBER_GOAL = 10000


def _ensure_growth_tables(cur=None):
    own = cur is None
    db = None
    if own:
        db = get_db()
        cur = db.cursor()
    try:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS group_activity (
                tg_id VARCHAR(32) NOT NULL,
                day DATE NOT NULL,
                PRIMARY KEY (tg_id, day),
                INDEX idx_day (day)
            )"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS group_member_snapshots (
                id INT AUTO_INCREMENT PRIMARY KEY,
                counted_at DATETIME NOT NULL,
                member_count INT NOT NULL,
                INDEX idx_counted (counted_at)
            )"""
        )
        if own:
            db.commit()
    finally:
        if own and db:
            db.close()


def _is_required_group_chat(chat: dict) -> bool:
    req = _telegram_required_chat()
    if not req or not chat:
        return False
    ctype = (chat.get("type") or "").lower()
    if ctype not in ("group", "supergroup"):
        return False
    uname = (chat.get("username") or "").lstrip("@").lower()
    cid = str(chat.get("id") or "")
    raw = req.lstrip("@")
    if raw.lstrip("-").isdigit():
        return cid == raw or cid == str(req)
    return uname == raw.lower()


def _telegram_member_count():
    """必加群当前人数。成功则顺带写快照。"""
    chat = _telegram_required_chat()
    if not chat:
        return None, "未配置必加群"
    cache_key = "growth:member_count"
    r = None
    try:
        r = get_redis()
        if r:
            cached = r.get(cache_key)
            if cached:
                return int(cached), None
    except Exception:
        r = None
    try:
        result = _telegram_api("getChatMemberCount", {"chat_id": chat}, timeout=12)
        count = int(result)
    except Exception as e:
        return None, str(e)[:160]
    try:
        if r:
            r.setex(cache_key, 600, str(count))
    except Exception:
        pass
    try:
        db = get_db()
        try:
            cur = db.cursor()
            _ensure_growth_tables(cur)
            cur.execute(
                "SELECT member_count, counted_at FROM group_member_snapshots ORDER BY id DESC LIMIT 1"
            )
            last = cur.fetchone()
            should = True
            if last:
                last_c = last.get("member_count")
                last_t = last.get("counted_at")
                if last_c == count and isinstance(last_t, datetime):
                    if datetime.now() - last_t < timedelta(hours=6):
                        should = False
            if should:
                cur.execute(
                    "INSERT INTO group_member_snapshots (counted_at, member_count) VALUES (NOW(), %s)",
                    (count,),
                )
                db.commit()
        finally:
            db.close()
    except Exception as e:
        app.logger.debug("member snapshot: %s", e)
    return count, None


def _telegram_user_in_required_group(telegram_id) -> tuple[bool, str]:
    """校验 TG 用户是否在必加群。未配置必加群时直接放行。
    返回 (ok, detail) detail 为 status 或错误说明。
    """
    chat = _telegram_required_chat()
    if not chat:
        return True, "disabled"
    tg_id = str(telegram_id or "").strip()
    if not tg_id:
        return False, "缺少 Telegram ID"
    # 超管仍建议在群里，但允许兜底以免把自己锁死
    if tg_id in _telegram_superadmin_ids():
        return True, "superadmin"
    if not _telegram_bot_token():
        return False, "未配置 Bot Token，无法校验群成员"
    try:
        member = _telegram_api("getChatMember", {
            "chat_id": chat,
            "user_id": int(tg_id),
        })
    except Exception as e:
        err = str(e)
        app.logger.warning("getChatMember failed chat=%s user=%s: %s", chat, tg_id, err)
        low = err.lower()
        if "participant_id_invalid" in low or "user not found" in low:
            return False, "not_member"
        if "chat not found" in low:
            return False, "群不存在或 Bot 未入群"
        return False, f"无法校验群成员（Bot 需为群管理员）: {err}"
    status = (member or {}).get("status") or ""
    # restricted 也可能仍在群内
    if status == "restricted" and member.get("is_member") is False:
        return False, "restricted"
    if status in ("creator", "administrator", "member", "restricted"):
        return True, status
    return False, status or "not_member"


TG_BIND_PREFIX = "tg_bind:"


TG_LOGIN_PREFIX = "tg_login:"


def _public_site_url():
    return (_telegram_webhook_base() or "https://vault.aaaaaaaa.host").rstrip("/")


def _tg_search_rate_ok(tg_id) -> bool:
    try:
        r = get_redis()
        if not r:
            return True
        key = f"tg_search:{tg_id}:{int(time.time()) // 60}"
        n = int(r.incr(key))
        if n == 1:
            r.expire(key, 70)
        return n <= 12
    except Exception:
        return True


def _bot_search_resources(query, limit=8):
    """站内 FULLTEXT 搜索，供 TG Bot 使用。"""
    display, ft = _sanitize_ft_query(query)
    if not display:
        return [], display
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """SELECT id, title, url, source, quality, type, year, password, note, keyword
               FROM resources
               WHERE MATCH(title, note) AGAINST(%s IN BOOLEAN MODE)
                 AND (link_status IS NULL OR link_status != 'dead')
               ORDER BY created_at DESC
               LIMIT 24""",
            (ft,),
        )
        blocked = [w.lower() for w in _get_cached_filter_words() if w and len(w) >= 2]
        qlow = display.lower()
        if any(w == qlow or w in qlow for w in blocked):
            return [], display
        items = []
        for row in cur.fetchall() or []:
            blob = f"{row.get('title') or ''} {row.get('note') or ''} {row.get('keyword') or ''}".lower()
            if any(w in blob for w in blocked):
                continue
            items.append(row)
            if len(items) >= limit:
                break
        return items, display
    finally:
        db.close()


def _format_tg_search_results(items, query):
    from urllib.parse import quote
    site = _public_site_url()
    qurl = f"{site}/?q={quote(query)}"
    if not items:
        return f"没找到「{query}」。\n到网站再搜：{qurl}"
    lines = [f"🔍 {query} · {len(items)} 条\n"]
    for i, it in enumerate(items, 1):
        title = (it.get("title") or it.get("keyword") or "未命名").strip()[:64]
        meta = " · ".join(
            str(x) for x in (it.get("year"), it.get("source"), it.get("quality")) if x
        )
        url = (it.get("url") or "").strip()
        pwd = (it.get("password") or "").strip()
        lines.append(f"{i}. {title}" + (f"\n{meta}" if meta else ""))
        if url:
            lines.append(url)
        if pwd:
            lines.append(f"提取码: {pwd}")
        lines.append("")
    lines.append(f"更多：{qurl}")
    return "\n".join(lines).strip()[:3900]


def _tg_log_search(query, tg_id):
    try:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO search_logs (keyword, ip, user_id) VALUES (%s, %s, %s)",
                (query[:190], f"tg:{tg_id}", None),
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


_VISIT_REDIS_KEY = "res_web:visits:total"


def _visit_today_key():
    return f"{_VISIT_DAILY_PREFIX}{datetime.now().strftime('%Y-%m-%d')}"


_FW_CACHE_KEY = "_filter_words"


_FW_CACHE_TTL = 300  # 300秒缓存


def _background_import(keyword, items, return_counts=False):
    """后台自动将在线搜索结果入库，跳过已标记失效的URL
    return_counts=True 时返回 (imported, skipped_dead) 元组，否则返回 None
    """
    try:
        db = get_db()
        cur = db.cursor()
        imported = 0
        skipped_dead = 0
        for it in items:
            url = it.get("url", "")
            if not url:
                continue
            # 检查 URL 是否已标记为失效
            try:
                cur.execute(
                    "SELECT id FROM resources WHERE url = %s AND link_status = 'dead' LIMIT 1",
                    (url,)
                )
                if cur.fetchone():
                    skipped_dead += 1
                    continue
            except Exception:
                pass
            try:
                cur.execute(
                    """INSERT IGNORE INTO resources
                    (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                    (
                        keyword,
                        it.get("source", ""),
                        url,
                        it.get("title", ""),
                        it.get("note", ""),
                        it.get("password", ""),
                        it.get("datetime", ""),
                        it.get("quality", ""),
                        it.get("type", ""),
                        it.get("year", 0),
                    ),
                )
                if cur.rowcount > 0:
                    imported += 1
            except Exception:
                continue
        db.commit()
        db.close()
        if imported > 0 or skipped_dead > 0:
            msg = f"[auto-import] '{keyword}': imported {imported}"
            if skipped_dead > 0:
                msg += f", skipped {skipped_dead} dead"
            print(msg)
        if return_counts:
            return imported, skipped_dead
    except Exception as e:
        print(f"[auto-import] error for '{keyword}': {e}")
        if return_counts:
            return 0, 0


def _search_online_sync(keyword, limit=20, timeout=15):
    """同步在线搜索：并发请求所有 import_api_url，合并去重返回。
    用于在线搜索兜底。"""
    import threading as _th
    import requests as _req
    urls = _get_import_api_urls()
    all_items = []
    lock = _th.Lock()

    def _fetch(url):
        try:
            resp = _req.post(url, json={"kw": keyword, "limit": limit}, timeout=timeout)
            data = resp.json()
            if data.get("code") != 0:
                return
            items = _parse_online_items(keyword, data)
            if items:
                with lock:
                    all_items.extend(items)
        except Exception:
            pass

    threads = [_th.Thread(target=_fetch, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)

    # 去重
    seen = set()
    result = []
    for it in all_items:
        u = it.get("url", "")
        if u and u not in seen:
            seen.add(u)
            result.append(it)
    return result
