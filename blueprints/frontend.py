"""frontend blueprint (split from app.py)."""


from flask import Blueprint


import re


import time


import secrets


import html as html_mod


from datetime import datetime, timedelta


import pymysql


import pymysql.cursors


import requests as http_requests


from flask import Flask, jsonify, request, send_from_directory, session, redirect


import json


from flask_wtf.csrf import CSRFProtect, generate_csrf


from .common import TG_BIND_TTL, _IMG_TTL, _admin_peer_guard, _async_online_search, _cache, _ensure_announcements_table, _ensure_telegram_id_column, _ensure_telegram_settings_keys, _ensure_telegram_webhook, _ensure_watchlist_table, _filter_words_condition, _get_cached_filter_words, _get_import_api_urls, _get_tmdb_api_key, _get_visit_counts, _growth_info, _img_cache, _img_cache_evict, _img_cache_lock, _img_proxy_url_ok, _img_redis_key, _incr_visit_counts, _invalidate_resource_cache, _is_foreign_ip, _is_staff_role, _is_superadmin_role, _json_default, _log_op, _make_cache_key, _mask_secrets, _merge_source, _norm_role, _safe_next_url, _should_run_online_search, _telegram_bot_token, _telegram_bot_username, _tg_bind_store, _user_pass_internal_beta_gate, admin_required, app, check_rate_limit, csrf, get_client_ip, get_db, get_redis, group_required_for_favorites, hash_password, redis_cached, verify_password


frontend_bp = Blueprint('frontend', __name__)


@frontend_bp.route("/api/img_proxy")
def img_proxy():
    """代理 TMDB 图片。三级缓存: 进程内存 -> Redis(跨 worker 共享) -> 回源 TMDB。

    注意: 早期版本只有进程内存缓存，但 gunicorn 跑 4 个 worker 是独立进程、
    内存不共享，同一张图最多要回源下载 4 次，首屏 20 张图实测需 8.8 秒。
    加 Redis 层后 4 个 worker 共享缓存。
    """
    import hashlib as _hl
    url = request.args.get("url", "")
    if not url:
        return "", 400
    ok, reason = _img_proxy_url_ok(url)
    if not ok:
        app.logger.warning("img_proxy 拒绝非白名单 URL (reason=%s): %.200s", reason, url)
        return "", 400
    now = time.time()

    def _respond(data, ct):
        etag = '"' + _hl.md5(data).hexdigest() + '"'
        if request.headers.get("If-None-Match") == etag:
            return "", 304
        return data, 200, {
            "Content-Type": ct,
            "Cache-Control": "public, max-age=604800, immutable",
            "ETag": etag,
        }

    # L1: 进程内存
    with _img_cache_lock:
        hit = _img_cache.get(url)
    if hit:
        data, ct, expire = hit
        if now < expire:
            return _respond(data, ct)

    # L2: Redis（跨 worker 共享）
    rkey = _img_redis_key(url)
    try:
        cached = get_redis().hgetall(rkey)
        if cached:
            data = cached.get(b"d")
            ct = (cached.get(b"c") or b"image/jpeg").decode()
            if data:
                with _img_cache_lock:
                    _img_cache[url] = (data, ct, now + _IMG_TTL)
                    _img_cache_evict()
                return _respond(data, ct)
    except Exception as e:
        app.logger.warning("img_proxy Redis 读取失败: %s: %s", type(e).__name__, e)

    # L3: 回源 TMDB
    # 禁止重定向：白名单域名仍可能 302 到内网/第三方，跟随等于绕过上面的 SSRF 校验
    try:
        import requests as _req
        r = _req.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"},
                     allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            app.logger.warning("img_proxy 回源重定向被拒 %s -> %.200s",
                               url, r.headers.get("Location", ""))
            return "", 400
        if r.status_code == 200:
            ct = r.headers.get("Content-Type", "image/jpeg")
            if not ct.lower().startswith("image/"):
                # 防止把 HTML/脚本等内容以图片名义代理回来
                app.logger.warning("img_proxy 回源非图片内容 (%s): %.200s", ct, url)
                return "", 400
            with _img_cache_lock:
                _img_cache[url] = (r.content, ct, now + _IMG_TTL)
                _img_cache_evict()
            try:
                pipe = get_redis().pipeline()
                pipe.hset(rkey, mapping={"d": r.content, "c": ct})
                pipe.expire(rkey, _IMG_TTL)
                pipe.execute()
            except Exception as e:
                app.logger.warning("img_proxy Redis 写入失败: %s: %s", type(e).__name__, e)
            return _respond(r.content, ct)
    except Exception as e:
        app.logger.warning("img_proxy 回源失败 %s: %s: %s", url, type(e).__name__, e)
    return "", 404


@frontend_bp.route("/api/user/telegram/bind-code", methods=["POST"])
def api_telegram_bind_code():
    """登录用户生成 Bot 深链绑定码"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    if not _telegram_bot_token() or not _telegram_bot_username():
        return jsonify({"error": "服务端未配置 Telegram Bot"}), 500

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT telegram_id FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        if row and row.get("telegram_id"):
            return jsonify({"error": "已绑定 Telegram，请先解绑", "telegram_id": row["telegram_id"]}), 400
    finally:
        db.close()

    ok, info = _ensure_telegram_webhook()
    if not ok:
        return jsonify({"error": f"Webhook 未就绪: {info}"}), 503

    code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    try:
        _tg_bind_store(code, uid)
    except Exception as e:
        app.logger.exception("api_telegram_bind_code failed")
        return jsonify({"error": str(e)}), 500

    bot = _telegram_bot_username()
    deep_link = f"https://t.me/{bot}?start=bind_{code}"
    return jsonify({
        "ok": True,
        "code": code,
        "deep_link": deep_link,
        "bot_username": bot,
        "expires_in": TG_BIND_TTL,
        "hint": f"打开 Bot 后发送 /start bind_{code}，或点击下方链接一键绑定",
    })


@frontend_bp.route("/api/user/telegram/unbind", methods=["POST"])
def api_telegram_unbind():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    _ensure_telegram_id_column()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE users SET telegram_id=NULL WHERE id=%s", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@frontend_bp.route("/")
def index():
    resp = send_from_directory(".", "index.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp


@frontend_bp.route("/api/user/me")
def api_user_me():
    """返回当前登录用户信息（角色以数据库为准，避免升超管后会话过期）。"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"logged_in": False, "in_group": False, "growth": _growth_info()})
    payload = {
        "logged_in": True,
        "user_id": uid,
        "username": session.get("username", ""),
        "is_admin": bool(session.get("is_admin")),
        "is_superadmin": bool(session.get("is_superadmin")),
        "role": session.get("role") or "user",
        "github_id": None,
        "telegram_id": None,
        "in_group": False,
        "growth": _growth_info(),
    }
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT username, role, github_id, telegram_id, status, points, checkin_streak, last_checkin_at, email, nickname FROM users WHERE id=%s",
            (uid,),
        )
        row = cur.fetchone()
        if row:
            if int(row.get("status") or 0) != 1:
                session.clear()
                return jsonify({"logged_in": False, "error": "账号已禁用"})
            role = _norm_role(row.get("role"))
            session["username"] = row.get("username") or session.get("username")
            session["role"] = role
            session["is_admin"] = _is_staff_role(role)
            session["is_superadmin"] = _is_superadmin_role(role)
            payload["username"] = session["username"]
            payload["nickname"] = row.get("nickname") or session["username"]
            payload["email"] = row.get("email") or ""
            payload["role"] = role
            payload["is_admin"] = session["is_admin"]
            payload["is_superadmin"] = session["is_superadmin"]
            payload["github_id"] = row.get("github_id")
            payload["telegram_id"] = row.get("telegram_id")
            payload["points"] = int(row.get("points") or 0)
            payload["checkin_streak"] = int(row.get("checkin_streak") or 0)
            last_dt = row.get("last_checkin_at")
            payload["last_checkin_at"] = str(last_dt) if last_dt else None
            
            # 判断今日是否已签到
            import datetime
            today_str = datetime.date.today().isoformat()
            payload["already_checked"] = bool(last_dt and str(last_dt).startswith(today_str))
            # 统计用户个人的收藏数、追剧单数、订阅数
            cur.execute("SELECT count(*) as c FROM favorites WHERE user_id=%s", (uid,))
            payload["favorites_count"] = cur.fetchone()["c"]
            cur.execute("SELECT count(*) as c FROM watchlist WHERE user_id=%s", (uid,))
            payload["watchlist_count"] = cur.fetchone()["c"]
            cur.execute("SELECT count(*) as c FROM subscriptions WHERE user_id=%s", (uid,))
            payload["subscriptions_count"] = cur.fetchone()["c"]
    except Exception:
        app.logger.exception("api_user_me failed")
        pass
    finally:
        db.close()
    ok, _ = _user_pass_internal_beta_gate(user_id=uid, telegram_id=payload.get("telegram_id"), role=payload.get("role"))
    payload["in_group"] = bool(ok)
    return jsonify(payload)


@frontend_bp.route("/api/site-settings")
def api_site_settings():
    """获取公开的网站设置（不需要登录）"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT `key`, `value` FROM settings WHERE `key` IN ('site_name', 'site_footer', 'announcement_title', 'announcement_content', 'announcement_type', 'announcement_active')")
        items = cur.fetchall()
        result = {item["key"]: item["value"] for item in items}
        return jsonify(result)
    finally:
        db.close()


@frontend_bp.route("/api/growth")
def api_growth():
    """公开引流信息：进群 / 搜片 Bot / 频道 / 网站。"""
    _ensure_telegram_settings_keys()
    return jsonify(_growth_info())


@frontend_bp.route("/api/stats")
@redis_cached(ttl=300, key_prefix="stats")
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


@frontend_bp.route("/api/visit", methods=["POST"])
@csrf.exempt
def api_visit_track():
    """首页访问打点：同浏览器 30 分钟内只计 1 次，防刷新刷量"""
    cookie_name = "vd_visit_dedupe"
    if request.cookies.get(cookie_name):
        total, today = _get_visit_counts()
        return jsonify({"ok": True, "counted": False, "total": total, "today": today})
    total, today = _incr_visit_counts()
    resp = jsonify({"ok": True, "counted": True, "total": total, "today": today})
    resp.set_cookie(
        cookie_name,
        "1",
        max_age=1800,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


@frontend_bp.route("/api/visit")
def api_visit_get():
    """读取累计/今日访问（无需登录）"""
    total, today = _get_visit_counts()
    return jsonify({"total": total, "today": today})


@frontend_bp.route("/api/src_tags")
def api_src_tags():
    """获取按关键词搜索结果的网盘来源分布"""
    query = request.args.get("q", "").strip()
    db = get_db()
    try:
        cur = db.cursor()
        if query:
            cur.execute(
                "SELECT source, COUNT(*) as cnt FROM resources "
                "WHERE (title LIKE %s OR keyword LIKE %s OR note LIKE %s) "
                "GROUP BY source ORDER BY cnt DESC",
                (f"%{query}%", f"%{query}%", f"%{query}%"),
            )
        else:
            cur.execute(
                "SELECT source, COUNT(*) as cnt FROM resources "
                "GROUP BY source ORDER BY cnt DESC"
            )
        tags = [{"source": r["source"], "cnt": r["cnt"]} for r in cur.fetchall()]
        return jsonify({"ok": True, "tags": tags})
    finally:
        db.close()


@frontend_bp.route("/api/resources")
def api_resources():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)
    query = request.args.get("q", "").strip()
    filter_type = request.args.get("filter", "all")
    source_filter = request.args.get("source", "all")

    cache_key = _make_cache_key("resources", query, filter_type, source_filter, page, per_page)
    try:
        cached = get_redis().get(cache_key)
        if cached:
            return jsonify(json.loads(cached))
    except Exception:
        app.logger.exception("api_resources failed")
        pass

    offset = (page - 1) * per_page
    db = get_db()
    try:
        cur = db.cursor()
        conditions, params = [], []
        use_fulltext = False

        if query:
            # FULLTEXT 命中 title/note（ft_title_note 索引）
            # 不用 OR keyword LIKE —— OR 会让 MySQL 放弃 FULLTEXT 索引走全表扫描
            use_fulltext = True
            conditions.append("MATCH(title, note) AGAINST(%s IN BOOLEAN MODE)")
            params.append(f"+{query}*")

            # 记录搜索日志
            try:
                log_db = get_db()
                log_cur = log_db.cursor()
                log_cur.execute(
                    "INSERT INTO search_logs (keyword, ip, user_id) VALUES (%s, %s, %s)",
                    (query, request.remote_addr, session.get("user_id"))
                )
                log_db.commit()
                log_db.close()
            except Exception:
                app.logger.exception("api_resources failed")
                pass

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

        # 过滤失效链接：link_status='dead' 的不展示
        conditions.append("(link_status IS NULL OR link_status != 'dead')")

        # COUNT 不带过滤词（74 个 NOT LIKE 全表扫 2.7s），过滤词只在列表里用
        # total 会比实际显示略多（含被过滤资源），首页可接受
        count_where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        count_params = list(params)

        # 过滤词：排除含过滤词的资源（仅列表查询用）
        f_sql, f_params = _filter_words_condition(cur)
        if f_sql:
            conditions.append(f_sql)
            params.extend(f_params)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # COUNT 单独长缓存（10 分钟）—— 首页不强求精确 total
        count_cache_key = _make_cache_key("rcount", query, filter_type, source_filter)
        total = None
        try:
            cached_total = get_redis().get(count_cache_key)
            if cached_total:
                total = int(cached_total)
        except Exception:
            app.logger.exception("api_resources failed")
            pass
        if total is None:
            cur.execute(f"SELECT COUNT(*) as total FROM resources {count_where}", count_params)
            total = cur.fetchone()["total"]
            try:
                get_redis().setex(count_cache_key, 600, str(total))
            except Exception:
                app.logger.exception("api_resources failed")
                pass
        total_pages = max(1, (total + per_page - 1) // per_page)

        if query:
            like = f"%{query}%"
            cur.execute(
                f"""SELECT id, keyword, source, url, title, note, password,
                       quality, type, year, rating, datetime, created_at
                FROM resources {where}
                ORDER BY
                  CASE
                    WHEN title LIKE %s THEN 0
                    WHEN IFNULL(note,'') LIKE %s THEN 1
                    WHEN keyword LIKE %s THEN 2
                    ELSE 3
                  END,
                  created_at DESC
                LIMIT %s OFFSET %s""",
                params + [like, like, like, per_page, offset],
            )
        else:
            cur.execute(
                f"""SELECT id, keyword, source, url, title, note, password,
                       quality, type, year, rating, datetime, created_at
                FROM resources {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s""",
                params + [per_page, offset],
            )
        # 访客（未登录）剥离网盘提取码；已登录用户不受影响。见审计 P0 REQUIRE_LOGIN=0
        items = _mask_secrets(cur.fetchall())

        payload = {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "importing": False,
        }
        # v20260807_153309 (Step1 重构): 简化在线搜索触发逻辑
        # 1) 不在 api_resources 副作用里跑 _auto_import_trigger（避免每次搜索都查 import_queue）
        # 2) 仅当 DB 0 条 或 Redis 状态缺失时 才启动在线搜索
        # 3) _should_run_online_search 已经有冷却逻辑（10 分钟内不重复搜）
        if query and total == 0 and _should_run_online_search(query):
            import threading as _t
            _t.Thread(target=_async_online_search, args=(query, per_page), daemon=True).start()
            payload["importing"] = True
        # v20260818: 缓存 30s → 300s，写库时失效；COUNT 单独 10 分钟
        try:
            get_redis().setex(cache_key, 300, json.dumps(payload, ensure_ascii=False, default=_json_default))
        except Exception as e:
            app.logger.warning("resources 缓存写入失败: %s: %s", type(e).__name__, e)
        return jsonify(payload)
    finally:
        db.close()


@frontend_bp.route("/api/sources")
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


@frontend_bp.route("/api/keywords")
@redis_cached(ttl=300, key_prefix="keywords")
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


@frontend_bp.route("/api/online_status")
def api_online_status():
    """查询在线搜索进度（前端轮询用）。
    返回 status: pending(尚未启动) | running | done | error
    """
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"status": "pending", "imported": 0})
    try:
        cache_key = f"online_search_status:{keyword}"
        cached = get_redis().get(cache_key)
        if cached:
            data = json.loads(cached)
            return jsonify(data)
    except Exception:
        app.logger.exception("api_online_status failed")
        pass
    return jsonify({"status": "pending", "imported": 0})


@frontend_bp.route("/api/online_results")
def api_online_results():
    """前端兼容接口：返回 DB 里在线搜索的结果（按 created_at DESC 取最近 N 条匹配 keyword 的）。
    不再从 Redis 缓存取原始 items，避免 UI 滑入逻辑走旁路。
    """
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"items": [], "total": 0, "ready": False})
    # 同时返回状态
    status = {"status": "pending", "imported": 0}
    try:
        cache_key = f"online_search_status:{keyword}"
        cached = get_redis().get(cache_key)
        if cached:
            status = json.loads(cached)
    except Exception:
        app.logger.exception("api_online_results failed")
        pass
    # 从 DB 查这次搜索匹配 keyword 的总数
    db = get_db()
    try:
        cur = db.cursor()
        # 在线搜索的记录 = created_at 在最近 60 秒内 + keyword 匹配
        cur.execute(
            """SELECT COUNT(*) AS c FROM resources
               WHERE keyword = %s AND created_at >= DATE_SUB(NOW(), INTERVAL 60 SECOND)""",
            (keyword,)
        )
        total = cur.fetchone()["c"]
        # 取前 50 条展示
        cur.execute(
            """SELECT id, keyword, source, url, title, note, password,
                   quality, type, year, rating, datetime, created_at
            FROM resources
            WHERE keyword = %s AND created_at >= DATE_SUB(NOW(), INTERVAL 60 SECOND)
            ORDER BY created_at DESC LIMIT 50""",
            (keyword,)
        )
        # 访客剥离提取码；见审计 P0 REQUIRE_LOGIN=0
        items = _mask_secrets(cur.fetchall())
        return jsonify({
            "items": items,
            "total": total,
            "ready": status.get("status") in ("done",),
            "status": status.get("status", "pending"),
            "imported": status.get("imported", 0),
        })
    finally:
        db.close()


@frontend_bp.route("/api/import", methods=["POST"])
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

    API_URL = _get_import_api_urls()[0]
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
                    app.logger.warning("api_import failed: %s", e)
                    results["errors"].append({"keyword": kw, "error": str(e)})
                    continue

            cur.execute("SELECT COUNT(*) as total FROM resources")
            results["db_total"] = cur.fetchone()["total"]

    finally:
        db.close()

    return jsonify(results)


@frontend_bp.route("/api/csrf-token")
def api_csrf_token():
    """返回 CSRF token 给前端"""
    return jsonify({"csrf_token": generate_csrf()})


@frontend_bp.route("/api/health")
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
        app.logger.exception("api_health failed")
        health["services"]["database"] = {"status": "error", "error": str(e)}
        health["status"] = "degraded"

    return jsonify(health)


@frontend_bp.route("/api/users")
@admin_required
def api_users():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    offset = (page - 1) * per_page

    db = get_db()
    try:
        cur = db.cursor(pymysql.cursors.DictCursor)
        # 会话角色与库同步（升超管后无需重登）
        my_id = session.get("user_id")
        if my_id:
            cur.execute("SELECT role, username FROM users WHERE id=%s", (my_id,))
            me_row = cur.fetchone()
            if me_row:
                role = _norm_role(me_row.get("role"))
                session["role"] = role
                session["is_admin"] = _is_staff_role(role)
                session["is_superadmin"] = _is_superadmin_role(role)
                if me_row.get("username"):
                    session["username"] = me_row["username"]

        me_super = bool(session.get("is_superadmin"))
        q = (request.args.get("q") or "").strip()
        role_f = (request.args.get("role") or "").strip()

        conds = []
        params_cnt = []
        if not me_super:
            conds.append("role NOT IN ('superadmin','super','root')")
        if q:
            conds.append("(username LIKE %s OR email LIKE %s OR telegram_id LIKE %s)")
            params_cnt.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        if role_f:
            conds.append("role = %s")
            params_cnt.append(role_f)

        where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
        cur.execute(f"SELECT COUNT(*) as total FROM users {where_sql}", params_cnt)
        total = cur.fetchone()["total"]

        cur.execute(
            f"""SELECT id, username, email, role, status, points, checkin_streak, last_login, created_at, updated_at, github_id, telegram_id
            FROM users {where_sql} ORDER BY id DESC LIMIT %s OFFSET %s""",
            params_cnt + [per_page, offset],
        )
        items = cur.fetchall()

        for i in items:
            i["last_login"] = str(i["last_login"]) if i["last_login"] else ""
            i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
            i["updated_at"] = str(i["updated_at"]) if i["updated_at"] else ""
            i["role"] = _norm_role(i.get("role"))

        # 用户看板全局统计指标
        cur.execute("""
            SELECT 
                COUNT(*) as total_users,
                SUM(CASE WHEN created_at >= CURDATE() THEN 1 ELSE 0 END) as today_new,
                SUM(CASE WHEN last_login >= CURDATE() THEN 1 ELSE 0 END) as today_active,
                SUM(CASE WHEN telegram_id IS NOT NULL AND telegram_id != '' THEN 1 ELSE 0 END) as tg_bound,
                SUM(CASE WHEN github_id IS NOT NULL AND github_id != '' THEN 1 ELSE 0 END) as gh_bound
            FROM users
        """)
        user_stats_raw = cur.fetchone() or {}
        user_stats = {
            "total_users": int(user_stats_raw.get("total_users") or 0),
            "today_new": int(user_stats_raw.get("today_new") or 0),
            "today_active": int(user_stats_raw.get("today_active") or 0),
            "tg_bound": int(user_stats_raw.get("tg_bound") or 0),
            "gh_bound": int(user_stats_raw.get("gh_bound") or 0),
        }

        return jsonify({
            "items": items,
            "total": total,
            "page": page,
            "stats": user_stats,
            "me": {
                "user_id": session.get("user_id"),
                "is_superadmin": me_super,
                "role": session.get("role") or ("superadmin" if me_super else "admin"),
            },
        })
    finally:
        db.close()


@frontend_bp.route("/api/users/<int:uid>", methods=["POST"])
@admin_required
def api_user_update(uid):
    """超管可改管理员含密码；普通管理员不能改管理员/超管。"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "无数据"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id, role FROM users WHERE id=%s", (uid,))
        target = cur.fetchone()
        if not target:
            return jsonify({"error": "用户不存在"}), 404
        my_id = session.get("user_id")
        me_super = bool(session.get("is_superadmin"))
        target_role = _norm_role(target.get("role"))

        if uid != my_id:
            err, code = _admin_peer_guard(uid, target_role)
            if err:
                return jsonify({"error": err}), code
        else:
            if "role" in data and _norm_role(data.get("role")) != target_role:
                return jsonify({"error": "不能改自己的角色"}), 403
            if "status" in data and int(data.get("status") or 0) != 1:
                return jsonify({"error": "不能禁用自己"}), 403

        new_role = _norm_role(data["role"]) if "role" in data else None
        if new_role == "superadmin" and not me_super:
            return jsonify({"error": "只有超管能设置超管角色"}), 403
        if target_role == "superadmin" and not me_super:
            return jsonify({"error": "不能修改超管"}), 403

        sets, vals = [], []
        if "username" in data:
            sets.append("username=%s"); vals.append(data["username"])
        if "email" in data:
            sets.append("email=%s"); vals.append(data["email"])
        if "password" in data and data["password"]:
            if not me_super and _is_staff_role(target_role) and uid != my_id:
                return jsonify({"error": "只有超管能改管理员密码"}), 403
            pw = str(data["password"])
            if len(pw) < 6:
                return jsonify({"error": "密码至少6位"}), 400
            sets.append("password=%s"); vals.append(hash_password(pw))
        if new_role:
            sets.append("role=%s"); vals.append(new_role)
        if "status" in data:
            sets.append("status=%s"); vals.append(data["status"])
        if "points" in data and data["points"] is not None:
            try:
                pts = int(data["points"])
                sets.append("points=%s"); vals.append(pts)
            except Exception:
                app.logger.exception("api_user_update failed")
                pass
        if "telegram_id" in data:
            if not me_super:
                return jsonify({"error": "只有超管能改 Telegram ID"}), 403
            tg = str(data.get("telegram_id") or "").strip()
            if tg and not tg.isdigit():
                return jsonify({"error": "Telegram ID 须为数字"}), 400
            sets.append("telegram_id=%s")
            vals.append(tg or None)
        if not sets:
            return jsonify({"error": "无有效字段"}), 400
        sets.append("updated_at=NOW()")
        vals.append(uid)
        cur.execute(f"UPDATE users SET {','.join(sets)} WHERE id=%s", vals)
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    except pymysql.err.IntegrityError as e:
        app.logger.exception("api_user_update failed")
        msg = str(e)
        if "telegram" in msg.lower():
            return jsonify({"error": "该 Telegram ID 已被其他账号绑定"}), 409
        return jsonify({"error": "用户名已存在"}), 409
    finally:
        db.close()


@frontend_bp.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_user_delete(uid):
    """超管可删管理员；不能删超管、不能删自己。普通管理员只能删普通用户。"""
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
        err, code = _admin_peer_guard(uid, u.get("role"))
        if err:
            return jsonify({"error": err}), code
        cur.execute("DELETE FROM users WHERE id=%s", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@frontend_bp.route("/api/users", methods=["POST"])
@admin_required
def api_user_add():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"error": "用户名不能为空"}), 400
    role = _norm_role(data.get("role", "user"))
    me_super = bool(session.get("is_superadmin"))
    if role == "superadmin" and not me_super:
        return jsonify({"error": "只有超管能创建超管"}), 403
    pw = (data.get("password") or "").strip()
    db = get_db()
    try:
        cur = db.cursor()
        if pw:
            if len(pw) < 6:
                return jsonify({"error": "密码至少6位"}), 400
            cur.execute(
                "INSERT INTO users (username, email, role, status, password) VALUES (%s, %s, %s, %s, %s)",
                (username, data.get("email", ""), role, 1, hash_password(pw)),
            )
        else:
            cur.execute(
                "INSERT INTO users (username, email, role, status) VALUES (%s, %s, %s, %s)",
                (username, data.get("email", ""), role, 1),
            )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    except pymysql.err.IntegrityError:
        app.logger.exception("api_user_add failed")
        return jsonify({"error": f'用户名 "{username}" 已存在'}), 409
    finally:
        db.close()


@frontend_bp.route("/api/users/<int:uid>/github", methods=["POST"])
@admin_required
def api_user_bind_github(uid):
    """管理员绑定/解绑 GitHub：传 {"github_id": "..."} 或 {"github_id": null}"""
    data = request.get_json() or {}
    github_id = data.get("github_id")
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT role FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()
        if not u:
            return jsonify({"error": "用户不存在"}), 404
        err, code = _admin_peer_guard(uid, u.get("role"))
        if err:
            return jsonify({"error": err}), code
        if github_id:
            cur.execute(
                "UPDATE users SET github_id=%s WHERE id=%s",
                (str(github_id), uid),
            )
        else:
            cur.execute(
                "UPDATE users SET github_id=NULL WHERE id=%s",
                (uid,),
            )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@frontend_bp.route("/api/user/bind_github", methods=["POST"])
@csrf.exempt
def api_user_bind_github_self():
    """当前登录用户解绑 GitHub（绑定请走 /login/github?bind=1 OAuth）"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    # 仅允许解绑；绑定必须走 GitHub OAuth，避免手填 ID
    if data.get("github_id") not in (None, "", False):
        return jsonify({"error": "请使用 GitHub 登录完成绑定", "oauth": "/login/github?bind=1&next=/profile"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE users SET github_id=NULL WHERE id=%s", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@frontend_bp.route("/api/user/profile", methods=["POST"])
@csrf.exempt
def api_user_update_profile():
    """更新个人资料（昵称、邮箱）"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    nickname = str(data.get("nickname") or "").strip()[:50]
    email = str(data.get("email") or "").strip()[:100]
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE users SET nickname=%s, email=%s, updated_at=NOW() WHERE id=%s", (nickname or None, email or None, uid))
        db.commit()
        return jsonify({"ok": True, "message": "个人资料已更新"})
    finally:
        db.close()


@frontend_bp.route("/api/user/change-password", methods=["POST"])
@csrf.exempt
def api_user_change_password():
    """用户自行修改密码"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    old_pass = str(data.get("old_password") or "").strip()
    new_pass = str(data.get("new_password") or "").strip()
    if not old_pass or not new_pass:
        return jsonify({"error": "请输入旧密码和新密码"}), 400
    if len(new_pass) < 6:
        return jsonify({"error": "新密码至少需要 6 个字符"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT password FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        if not row or not row.get("password"):
            return jsonify({"error": "用户状态异常"}), 400
        if not verify_password(old_pass, row["password"]):
            return jsonify({"error": "原密码不正确"}), 400
        new_hash = hash_password(new_pass)
        cur.execute("UPDATE users SET password=%s, updated_at=NOW() WHERE id=%s", (new_hash, uid))
        db.commit()
        return jsonify({"ok": True, "message": "密码修改成功"})
    finally:
        db.close()


@frontend_bp.route("/terms")
@frontend_bp.route("/disclaimer")
def page_terms():
    """使用条款与免责声明页面"""
    return send_from_directory(app.root_path, "terms.html")


@frontend_bp.route("/api/user/checkin/status")
def api_user_checkin_status():
    """获取当前用户的签到状态与连续签到统计"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"logged_in": False, "already_checked": False, "points": 0, "streak": 0})
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT points, checkin_streak, last_checkin_at FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"logged_in": False, "already_checked": False, "points": 0, "streak": 0})
        import datetime
        today = datetime.date.today()
        last_checkin = row.get("last_checkin_at")
        already_checked = (last_checkin == today)
        streak = int(row.get("checkin_streak") or 0)
        # 计算今日若签到可得积分
        next_reward = min(10 + streak * 5, 50) if already_checked else min(10 + (streak if last_checkin == today - datetime.timedelta(days=1) else 0) * 5, 50)
        return jsonify({
            "logged_in": True,
            "already_checked": already_checked,
            "points": int(row.get("points") or 0),
            "streak": streak,
            "next_reward": next_reward,
            "last_checkin_at": str(last_checkin) if last_checkin else None,
        })
    finally:
        db.close()


@frontend_bp.route("/api/user/points/logs")
def api_user_point_logs():
    """获取用户个人积分变动明细列表"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"items": [], "total": 0})
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, action, change_amount, balance_after, remark, created_at "
            "FROM point_logs WHERE user_id=%s ORDER BY id DESC LIMIT 50",
            (uid,),
        )
        logs = cur.fetchall()
        for l in logs:
            l["created_at"] = str(l["created_at"]) if l.get("created_at") else ""
        return jsonify({"items": logs, "total": len(logs)})
    finally:
        db.close()


@frontend_bp.route("/api/user/checkin", methods=["POST"])
@csrf.exempt
def api_user_checkin():
    """每日签到领积分（防重入、阶梯奖励、流水记录）"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT points, checkin_streak, last_checkin_at FROM users WHERE id=%s FOR UPDATE", (uid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "用户不存在"}), 404
        import datetime
        today = datetime.date.today()
        last_checkin = row.get("last_checkin_at")
        if last_checkin == today:
            return jsonify({
                "error": "今天已经签过到啦，明天再来吧！",
                "points": row.get("points", 0),
                "streak": row.get("checkin_streak", 0),
                "already_checked": True,
            }), 400
        streak = int(row.get("checkin_streak") or 0)
        yesterday = today - datetime.timedelta(days=1)
        if last_checkin == yesterday:
            streak += 1
        else:
            streak = 1
        # 基础 10 积分 + 连续签到递增（每多1天+5分，上限 50 分）
        reward = min(10 + (streak - 1) * 5, 50)
        curr = int(row.get("points") or 0)
        new_bal = curr + reward
        cur.execute(
            "UPDATE users SET points=%s, checkin_streak=%s, last_checkin_at=%s, updated_at=NOW() WHERE id=%s",
            (new_bal, streak, today, uid),
        )
        cur.execute(
            "INSERT INTO point_logs (user_id, action, change_amount, balance_after, remark, created_at) "
            "VALUES (%s, 'checkin', %s, %s, %s, NOW())",
            (uid, reward, new_bal, f"每日签到 (连续第 {streak} 天)"),
        )
        db.commit()
        return jsonify({
            "ok": True,
            "message": f"签到成功！获得 +{reward} 积分，已连续签到 {streak} 天！",
            "reward": reward,
            "points": new_bal,
            "streak": streak,
            "already_checked": True,
        })
    finally:
        db.close()


@frontend_bp.route("/api/user/bind-email", methods=["POST"])
@csrf.exempt
def api_user_bind_email():
    """个人中心：绑定 / 换绑邮箱（需验证码）"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "error": "请先登录"}), 401

    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()

    if not email or not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        return jsonify({"ok": False, "error": "请输入有效的电子邮箱地址"}), 400
    if not code or len(code) != 6:
        return jsonify({"ok": False, "error": "请输入6位有效数字验证码"}), 400

    r = get_redis()
    otp_key = f"res_web:email_otp:{email}:bind"
    cached_code = r.get(otp_key).decode("utf-8") if (r and r.get(otp_key)) else None

    if not cached_code or cached_code != code:
        return jsonify({"ok": False, "error": "验证码错误或已过期，请重新获取"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id FROM users WHERE email=%s AND id!=%s LIMIT 1", (email, uid))
        if cur.fetchone():
            return jsonify({"ok": False, "error": "该邮箱已被其他账号绑定"}), 400

        cur.execute("UPDATE users SET email=%s, updated_at=NOW() WHERE id=%s", (email, uid))
        db.commit()

        if r:
            r.delete(otp_key)

        return jsonify({"ok": True, "message": "邮箱绑定成功！", "email": email})
    finally:
        db.close()


@frontend_bp.route("/api/announcements")
def api_announcements():
    """获取当前生效的公告（公开），国内访问自动过滤敏感 TG 提示文案"""
    # 历史遗漏：announcements 表从未建过，导致首页每次加载必现 500（见审计）。
    _ensure_announcements_table()
    client_ip = get_client_ip()
    is_foreign = _is_foreign_ip(client_ip)

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, title, content, type FROM announcements WHERE active=1 AND (start_time IS NULL OR start_time <= NOW()) AND (end_time IS NULL OR end_time >= NOW()) ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return jsonify({})

        content = str(row["content"] or "")
        # 国内访问时，自动净化过滤 TG / Bot 敏感提示
        if not is_foreign:
            content = content.replace("先进本群，私聊 @vaultdrive_bot 发片名即可搜，或", "")
            content = content.replace("私聊 @vaultdrive_bot 发片名即可搜，或", "")
            content = content.replace("先进本群，私聊 @vaultdrive_bot 发片名即可搜，", "")
            content = content.replace("私聊 @vaultdrive_bot 发片名即可搜", "")
            content = content.replace("先进本群，", "")
            content = content.strip()

        return jsonify({
            "id": row["id"],
            "title": row["title"],
            "content": content,
            "type": row["type"],
        })
    finally:
        db.close()


@frontend_bp.route("/api/me")
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
    role = _norm_role(u["role"])
    session["is_admin"] = _is_staff_role(role)
    session["is_superadmin"] = _is_superadmin_role(role)
    session["role"] = role
    session["username"] = u["username"]
    return jsonify({
        "logged_in": True,
        "user_id": uid,
        "username": u["username"],
        "role": role,
        "is_admin": _is_staff_role(role),
        "is_superadmin": _is_superadmin_role(role),
    })


@frontend_bp.route("/api/change_password", methods=["POST"])

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


@frontend_bp.route("/api/favorites")
@group_required_for_favorites
def api_favorites():
    uid = session.get("user_id")
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT f.id, f.resource_id, f.created_at, r.title, r.source, r.url, r.password, r.type, r.quality FROM favorites f LEFT JOIN resources r ON f.resource_id = r.id WHERE f.user_id = %s ORDER BY f.created_at DESC", (uid,))
        items = cur.fetchall()
        for i in items: i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
        return jsonify({"items": items, "total": len(items)})
    finally: db.close()


@frontend_bp.route("/api/favorites", methods=["POST"])
@csrf.exempt
@group_required_for_favorites
def api_fav_add():
    uid = session.get("user_id")
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


@frontend_bp.route("/api/favorites/<int:rid>", methods=["DELETE"])
@csrf.exempt
@group_required_for_favorites
def api_fav_del(rid):
    uid = session.get("user_id")
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM favorites WHERE user_id = %s AND resource_id = %s", (uid, rid))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()


@frontend_bp.route("/api/watchlist")
@group_required_for_favorites
def api_watchlist():
    uid = session.get("user_id")
    _ensure_watchlist_table()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, tmdb_id, media_type, title, poster, year, rating, overview, created_at "
            "FROM watchlist WHERE user_id=%s ORDER BY created_at DESC",
            (uid,),
        )
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""
        return jsonify({"items": items, "total": len(items)})
    finally:
        db.close()


@frontend_bp.route("/api/watchlist", methods=["POST"])
@csrf.exempt
@group_required_for_favorites
def api_watchlist_add():
    uid = session.get("user_id")
    data = request.get_json() or {}
    tmdb_id = data.get("tmdb_id")
    if not tmdb_id:
        return jsonify({"error": "缺少 tmdb_id"}), 400
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        app.logger.exception("api_watchlist_add failed")
        return jsonify({"error": "tmdb_id 无效"}), 400
    media_type = (data.get("media_type") or "movie").strip()[:16] or "movie"
    title = (data.get("title") or "").strip()[:255]
    if not title:
        return jsonify({"error": "缺少标题"}), 400
    poster = (data.get("poster") or "")[:2000]
    year = str(data.get("year") or "")[:8]
    rating = str(data.get("rating") or "")[:16]
    overview = (data.get("overview") or "")[:2000]

    _ensure_watchlist_table()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO watchlist (user_id, tmdb_id, media_type, title, poster, year, rating, overview)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              title=VALUES(title), poster=VALUES(poster), year=VALUES(year),
              rating=VALUES(rating), overview=VALUES(overview)
            """,
            (uid, tmdb_id, media_type, title, poster, year, rating, overview),
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@frontend_bp.route("/api/watchlist/<int:tmdb_id>", methods=["DELETE"])
@csrf.exempt
@group_required_for_favorites
def api_watchlist_del(tmdb_id):
    uid = session.get("user_id")
    media_type = (request.args.get("media_type") or "movie").strip()[:16] or "movie"
    _ensure_watchlist_table()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "DELETE FROM watchlist WHERE user_id=%s AND tmdb_id=%s AND media_type=%s",
            (uid, tmdb_id, media_type),
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@frontend_bp.route("/api/search_history")
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


@frontend_bp.route("/api/search_history", methods=["POST"])
@csrf.exempt
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


@frontend_bp.route("/api/search_history", methods=["DELETE"])
@csrf.exempt
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


@frontend_bp.route("/api/click/<int:rid>", methods=["POST"])
def api_click(rid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE resources SET click_count = click_count + 1 WHERE id = %s", (rid,))
        db.commit()
        _invalidate_resource_cache()
        return jsonify({"ok": True})
    finally: db.close()


@frontend_bp.route("/profile")
@frontend_bp.route("/profile.html")
def profile_page():
    if not session.get("user_id"):
        return redirect(f"/login?next={_safe_next_url(request.path)}")
    resp = send_from_directory(".", "profile.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp


@frontend_bp.route("/api/search_log", methods=["POST"])
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
        blocked = [w.lower() for w in _get_cached_filter_words()]
        if any(b in kw.lower() for b in blocked):
            _log_op(uid, session.get("username"), "search_blocked", f"关键词: {kw}", ip)
            return jsonify({"ok": False, "blocked": True}), 200
        # 记录搜索
        cur.execute("INSERT INTO search_logs (user_id, keyword, ip) VALUES (%s, %s, %s)", (uid, kw, ip))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@frontend_bp.route("/api/hot_searches")
@redis_cached(ttl=300, key_prefix="hot_searches")
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


@frontend_bp.route("/api/autocomplete")
def api_autocomplete():
    """输入时返回匹配的搜索建议（搜索建议 + 资源库关键词 + 拼音）"""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 1:
        return jsonify({"items": []})
    db = get_db()
    try:
        cur = db.cursor()
        # 先查搜索建议表
        cur.execute(
            "SELECT keyword, hit_count FROM search_suggestions "
            "WHERE keyword LIKE %s ORDER BY hit_count DESC LIMIT 8",
            (f"%{q}%",)
        )
        items = list(cur.fetchall())
        seen = {it["keyword"] for it in items}
        # 补充资源库高频关键词
        if len(items) < 8:
            if seen:
                placeholders = ",".join(["%s"] * len(seen))
                cur.execute(
                    f"SELECT keyword, COUNT(*) as hit_count FROM resources "
                    f"WHERE keyword LIKE %s AND keyword NOT IN ({placeholders}) "
                    f"GROUP BY keyword ORDER BY hit_count DESC LIMIT %s",
                    [f"%{q}%"] + list(seen) + [8 - len(items)]
                )
            else:
                cur.execute(
                    "SELECT keyword, COUNT(*) as hit_count FROM resources "
                    "WHERE keyword LIKE %s "
                    "GROUP BY keyword ORDER BY hit_count DESC LIMIT %s",
                    (f"%{q}%", 8 - len(items))
                )
            for r in cur.fetchall():
                if r["keyword"] not in seen:
                    items.append(r)
                    seen.add(r["keyword"])
        # 纯拉丁字母：尝试拼音首字母/全拼匹配
        if len(items) < 8 and re.fullmatch(r"[A-Za-z0-9]{1,20}", q):
            try:
                from services import home_feed as hf
                for r in hf.pinyin_candidates(cur, q, limit=8 - len(items)):
                    if r["keyword"] not in seen:
                        items.append(r)
                        seen.add(r["keyword"])
            except Exception:
                app.logger.exception("api_autocomplete failed")
                pass
        return jsonify({"items": items[:8]})
    finally:
        db.close()


@frontend_bp.route("/api/resource/<int:rid>/report_dead", methods=["POST"])
@csrf.exempt
def api_report_dead(rid):
    client_ip = request.remote_addr or "unknown"
    r = get_redis()
    limit_key = f"res_web:report_limit:{client_ip}:{rid}"
    try:
        if r and r.get(limit_key):
            return jsonify({"ok": False, "error": "您已反馈过该链接，请勿频繁提交"}), 429
    except Exception:
        app.logger.exception("api_report_dead failed")
        pass

    cnt = 1
    try:
        if r:
            r.setex(limit_key, 1800, "1")
            cnt = r.incr(f"res_web:report_cnt:{rid}")
    except Exception:
        app.logger.exception("api_report_dead failed")
        pass

    if cnt >= 2:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "UPDATE resources SET link_status='suspect' WHERE id=%s AND (link_status IS NULL OR link_status != 'dead')",
                (rid,)
            )
            db.commit()
        except Exception:
            app.logger.exception("api_report_dead failed")
            pass
        finally:
            db.close()

    return jsonify({"ok": True, "message": "感谢反馈！已记录并加入优先复检队列", "reports": cnt})


@frontend_bp.route("/api/resource/<int:rid>/detail")
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
        # 访客剥离提取码；见审计 P0 REQUIRE_LOGIN=0
        item = _mask_secrets(item)
        return jsonify({"item": item})
    finally:
        db.close()


@frontend_bp.route("/api/tmdb/discover")
def api_tmdb_discover():
    """TMDB Discover API - 按类型/年代等筛选（socket直连）"""
    import socket, ssl as _ssl, json as _json, urllib.parse
    API_KEY = _get_tmdb_api_key()
    media_type = request.args.get("type", "movie")
    genre = request.args.get("genre", "").strip()
    page = request.args.get("page", "1")
    sort = request.args.get("sort", "popularity.desc").strip() or "popularity.desc"
    vote = request.args.get("vote", "").strip()
    orig_lang = request.args.get("with_original_language", "").strip()

    cache_key = f"tmdb_discover:{media_type}:{genre}:{page}:{sort}:{vote}:{orig_lang}"
    now = time.time()
    if cache_key in _cache:
        cached_data, expire = _cache[cache_key]
        if now < expire:
            return jsonify(cached_data)

    params = f"api_key={API_KEY}&language=zh-CN&sort_by={sort}&page={page}"
    if vote:
        params += f"&vote_count.gte={vote}"
    else:
        params += "&vote_count.gte=50"
    if genre:
        params += f"&with_genres={genre}"
    if orig_lang:
        params += f"&with_original_language={orig_lang}"
    path = f"/3/discover/{media_type}?{params}"

    try:
        ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
        s = socket.create_connection((str(ip), 443), timeout=5)
        s.settimeout(5)
        ss = _ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
        ss.settimeout(5)
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
            if cache_key in _cache:
                return jsonify(_cache[cache_key][0])
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
                except:
                    app.logger.warning("api_tmdb_discover failed")
                    break
                if sz == 0:
                    break
                body += r2[e + 2:e + 2 + sz]
                r2 = r2[e + 2 + sz + 2:]
        d = _json.loads(body)
        items = []
        for it in d.get("results", [])[:20]:
            poster_path = it.get("poster_path", "")
            backdrop_path = it.get("backdrop_path", "")
            items.append({
                "tmdb_id": it.get("id"),
                "title": it.get("title") or it.get("name", ""),
                "year": (it.get("release_date") or it.get("first_air_date") or "")[:4],
                "rating": round(it.get("vote_average", 0), 1),
                "overview": it.get("overview", ""),
                "media_type": media_type,
                "poster": "/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w342" + poster_path) if poster_path else "",
                "backdrop": "/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w780" + backdrop_path) if backdrop_path else ""
            })
        result = {"items": items, "total": d.get("total_results", 0)}
        _cache[cache_key] = (result, now + 1800)
        return jsonify(result)
    except Exception as e:
        app.logger.exception("api_tmdb_discover failed")
        if cache_key in _cache:
            return jsonify(_cache[cache_key][0])
        return jsonify({"error": str(e), "items": []}), 500


@frontend_bp.route("/api/tmdb/trending")
def api_tmdb_trending():
    """TMDB 热门排行（绕GFW直连），带缓存+超时+故障降级"""
    import socket, ssl as _ssl, json as _json, urllib.parse
    # 兼容前端 type 参数：week/day/movie/tv
    req_type = request.args.get("type", "")
    if req_type in ("movie", "tv"):
        media = req_type
        time_window = "week"
    elif req_type in ("day",):
        media = "all"
        time_window = "day"
    else:
        media = request.args.get("media", "all")
        time_window = request.args.get("time", "week")
    page = min(int(request.args.get("page", 1)), 10)
    API_KEY = _get_tmdb_api_key()
    TMDB_TTL = 1800  # 30 minutes cache

    # 生成缓存 key
    cache_key = f"tmdb_trending:{media}:{time_window}:{page}"
    rkey = _make_cache_key("tmdb_trending", media, time_window, page)
    now = time.time()

    # 检查缓存：Redis(跨worker共享) → 进程内 _cache(兜底)
    try:
        cached = get_redis().get(rkey)
        if cached:
            result_data = json.loads(cached)
            # 顺带回填进程内缓存，减少 Redis 往返
            _cache[cache_key] = (result_data, now + TMDB_TTL)
            return jsonify(result_data)
    except Exception:
        app.logger.exception("api_tmdb_trending failed")
        pass
    if cache_key in _cache:
        cached_data, expire = _cache[cache_key]
        if now < expire:
            return jsonify(cached_data)

    ep = f"trending/{media}/{time_window}"
    path = f"/3/{ep}?api_key={API_KEY}&language=zh-CN&page={page}"
    try:
        ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
        s = socket.create_connection((str(ip), 443), timeout=5)
        s.settimeout(5)  # 读取也限5秒
        ss = _ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
        ss.settimeout(5)
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
            # 无响应头，降级返回缓存或空
            if cache_key in _cache:
                return jsonify(_cache[cache_key][0])
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
                    app.logger.warning("api_tmdb_trending failed")
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
                "overview": (it.get("overview") or "")[:280],
                "poster": ("/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w342" + it["poster_path"])) if it.get("poster_path") else "",
                "backdrop": ("/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w780" + it["backdrop_path"])) if it.get("backdrop_path") else "",
                "rating": round(it.get("vote_average", 0), 1),
                "media_type": it.get("media_type", ""),
                "year": (it.get("release_date") or it.get("first_air_date") or "")[:4],
                "tmdb_id": it.get("id"),
                "genre_ids": it.get("genre_ids") or [],
            })
        result_data = {"items": items, "total": data.get("total_results", 0)}
        # 写入缓存（存数据字典，不存response对象，防止gzip中间件重复压缩）
        _cache[cache_key] = (result_data, now + TMDB_TTL)
        try:
            get_redis().setex(rkey, TMDB_TTL, json.dumps(result_data, default=_json_default))
        except Exception:
            app.logger.exception("api_tmdb_trending failed")
            pass  # Redis 写入失败不影响响应
        return jsonify(result_data)
    except Exception as e:
        # 外部API故障，返回缓存的旧数据
        app.logger.exception("api_tmdb_trending failed")
        try:
            cached = get_redis().get(rkey)
            if cached:
                result_data = json.loads(cached)
                _cache[cache_key] = (result_data, now + TMDB_TTL)
                return jsonify(result_data)
        except Exception:
            app.logger.exception("api_tmdb_trending failed")
            pass
        if cache_key in _cache:
            return jsonify(_cache[cache_key][0])
        return jsonify({"items": [], "error": str(e)})


@frontend_bp.route("/api/recommendations")
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
                    app.logger.warning("api_recommendations failed")
                    it["poster"] = imgs
            else:
                it["poster"] = ""
        return jsonify({"items": local_items, "strategy": "local_with_posters"})
    finally:
        db.close()


@frontend_bp.route("/api/subscriptions")
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


@frontend_bp.route("/api/subscriptions", methods=["POST"])
@csrf.exempt
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


@frontend_bp.route("/api/subscriptions/<int:sid>", methods=["DELETE"])
@csrf.exempt
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


@frontend_bp.route("/api/resource/<int:rid>/share", methods=["POST"])
@csrf.exempt
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


@frontend_bp.route("/s/<token>")
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
            poster_html = '<img src="' + html_mod.escape(poster, quote=True) + '" style="max-width:200px;border-radius:8px;margin-bottom:1rem">'

        quality_html = ""
        if quality:
            quality_html = '<span class="tag">' + html_mod.escape(quality) + '</span>'

        pw_html = ""
        if password:
            pw_html = '<div class="pw">🔑 提取码: ' + html_mod.escape(password) + '</div>'

        html_content = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + html_mod.escape(title) + ' - 分享</title>'
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
            + '<h2>' + html_mod.escape(title) + '</h2>'
            '<div><span class="tag">' + html_mod.escape(source) + '</span>' + quality_html + '</div>'
            '<div class="url" id="u">' + html_mod.escape(url) + '</div>'
            + pw_html
            + '<button class="btn" onclick="copyU()">📋 复制链接</button>'
            '<a class="btn btn2" href="' + html_mod.escape(url, quote=True) + '" target="_blank">🔗 打开网盘</a>'
            '<a class="btn btn2" href="/" style="text-decoration:none">🏠 更多资源</a>'
            '<script>function copyU(){var u=document.getElementById("u").innerText;'
            'navigator.clipboard.writeText(u).then(function(){alert("已复制！")})}</script>'
            '</div></body></html>'
        )
        return html_content
    finally:
        db.close()


@frontend_bp.route("/api/docs")
@admin_required
def api_docs():
    # P0-1(2026-10-16): 曾公开暴露 156 个接口清单，收敛为管理员可见
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


@frontend_bp.route("/api/import_status")
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


@frontend_bp.route("/api-docs")
@frontend_bp.route("/api-docs.html")
def api_docs_page():
    return send_from_directory(".", "api-docs.html")


@frontend_bp.route("/api/home/posters", methods=["POST"])
@csrf.exempt
def api_home_posters():
    """批量给片名补封面。body: {titles:[...]}"""
    data = request.get_json(silent=True) or {}
    titles = data.get("titles") or []
    if not isinstance(titles, list):
        return jsonify({"items": {}}), 400
    titles = [str(t)[:80] for t in titles if t][:24]
    try:
        from services import tmdb_poster as tp
        items = tp.lookup_posters(titles, get_redis(), _get_tmdb_api_key())
        return jsonify({"items": items})
    except Exception as e:
        app.logger.warning("home posters: %s", e)
        return jsonify({"items": {}, "error": str(e)})


@frontend_bp.route("/api/home/availability", methods=["POST"])
@csrf.exempt
def api_home_availability():
    """批量片名有源检测。body: {titles:[...]}"""
    data = request.get_json(silent=True) or {}
    titles = data.get("titles") or []
    if not isinstance(titles, list):
        return jsonify({"error": "titles must be list"}), 400
    titles = [str(t)[:80] for t in titles if t][:40]
    db = get_db()
    try:
        from services import home_feed as hf
        cur = db.cursor()
        return jsonify({"items": hf.availability_for_titles(cur, titles)})
    except Exception as e:
        app.logger.exception("api_home_availability failed")
        return jsonify({"items": {}, "error": str(e)}), 500
    finally:
        db.close()


@frontend_bp.route("/api/home/personal")
def api_home_personal():
    """登录用户个性化摘要。"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"login": False, "subscriptions": [], "watchlist_ready": [],
                        "favorites_count": 0, "subscription_new_total": 0})
    db = get_db()
    try:
        from services import home_feed as hf
        cur = db.cursor()
        summary = hf.personal_summary(cur, int(uid))
        summary["login"] = True
        return jsonify(summary)
    except Exception as e:
        app.logger.exception("api_home_personal failed")
        return jsonify({"login": True, "error": str(e), "subscriptions": [],
                        "watchlist_ready": [], "favorites_count": 0,
                        "subscription_new_total": 0}), 500
    finally:
        db.close()


@frontend_bp.route("/api/home/title_detail")
def api_home_title_detail():
    """详情抽屉：按片名聚合本站链接。"""
    title = (request.args.get("title") or "").strip()
    if not title:
        return jsonify({"error": "缺少 title"}), 400
    db = get_db()
    try:
        from services import home_feed as hf
        cur = db.cursor()
        return jsonify(hf.title_detail(cur, title, limit=40))
    except Exception as e:
        app.logger.exception("api_home_title_detail failed")
        return jsonify({"error": str(e), "items": []}), 500
    finally:
        db.close()


@frontend_bp.route("/api/resources/aggregate")
def api_resources_aggregate():
    q = (request.args.get("q") or "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    db = get_db()
    try:
        from services import dedup as dedup_svc
        cur = db.cursor()
        return jsonify(dedup_svc.list_aggregate(cur, q=q, page=page, per_page=per_page))
    except Exception as e:
        app.logger.exception("api_resources_aggregate failed")
        return jsonify({"items": [], "total": 0, "error": str(e)}), 500
    finally:
        db.close()


@frontend_bp.route("/api/subscriptions/new_count")
def api_subscriptions_new_count():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"login": False, "total": 0, "items": []})
    db = get_db()
    try:
        from services import home_feed as hf
        cur = db.cursor()
        summary = hf.personal_summary(cur, int(uid))
        return jsonify({
            "login": True,
            "total": summary.get("subscription_new_total") or 0,
            "items": summary.get("subscriptions") or [],
        })
    except Exception as e:
        app.logger.exception("api_subscriptions_new_count failed")
        return jsonify({"login": True, "total": 0, "items": [], "error": str(e)}), 500
    finally:
        db.close()


@frontend_bp.route("/site-settings.js")
def site_settings_js():
    return send_from_directory(".", "site-settings.js")


@frontend_bp.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


@frontend_bp.route("/manifest.webmanifest")
def web_manifest():
    return send_from_directory(".", "manifest.webmanifest",
                               mimetype="application/manifest+json")


@frontend_bp.route("/sw.js")
def service_worker():
    resp = send_from_directory(".", "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@frontend_bp.route("/favicon.ico")
@frontend_bp.route("/favicon.svg")
def favicon():
    """返回 VaultDrive 自定义 SVG favicon（金色渐变 + 闪电 ⚡）"""
    return send_from_directory(".", "favicon.svg", mimetype="image/svg+xml")


@frontend_bp.route("/robots.txt")
def robots_txt():
    """搜索引擎爬虫规则：允许收录首页，屏蔽后台/API/登录等私有路径。"""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "Disallow: /login\n"
        "Disallow: /profile\n"
        "Sitemap: https://vault.aaaaaaaa.host/sitemap.xml\n"
    )
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@frontend_bp.route("/sitemap.xml")
def sitemap_xml():
    """站点地图。当前为 SPA，无独立资源详情页，仅收录首页与公开静态页。"""
    base = "https://vault.aaaaaaaa.host"
    lastmod = datetime.now().strftime("%Y-%m-%d")
    urls = [
        ("/", "1.0", "daily"),
        ("/api-docs.html", "0.3", "monthly"),
    ]
    items = "\n".join(
        f"  <url><loc>{base}{p}</loc><lastmod>{lastmod}</lastmod>"
        f"<changefreq>{freq}</changefreq><priority>{pri}</priority></url>"
        for p, pri, freq in urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{items}\n"
        "</urlset>\n"
    )
    return xml, 200, {"Content-Type": "application/xml; charset=utf-8"}
