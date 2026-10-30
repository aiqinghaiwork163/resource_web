"""admin blueprint (split from app.py)."""


from flask import Blueprint


import time


import pymysql


import pymysql.cursors


import requests as http_requests


from flask import Flask, jsonify, request, send_from_directory, session, redirect


import json


import threading


from concurrent.futures import ThreadPoolExecutor


from .common import DB_CONFIG, _auto_scan_unchecked_bg, _bg_scan_state, _bg_scan_stop_event, _bg_scan_worker_thread, _cache, _check_single_link, _clear_filter_words_cache, _cluster_workers_online, _ensure_telegram_settings_keys, _ensure_telegram_webhook, _get_cached_filter_words, _get_import_api_urls, _get_visit_counts, _growth_metrics, _invalidate_resource_cache, _log_op, _mask_secret, _telegram_api, _telegram_bot_token, _telegram_bot_username, _telegram_required_chat, _telegram_superadmin_ids, _telegram_webhook_base, add_user_points, admin_required, app, csrf, get_db, get_redis


admin_bp = Blueprint('admin', __name__)


@admin_bp.route("/api/admin/telegram/webhook", methods=["POST"])
@admin_required
def api_admin_telegram_webhook():
    """管理员手动刷新 webhook"""
    r = get_redis()
    if r is not None:
        try:
            r.delete("tg_webhook:ready")
            r.delete("tg_webhook:ready:v3")
        except Exception:
            app.logger.exception("api_admin_telegram_webhook failed")
            pass
    ok, info = _ensure_telegram_webhook()
    return jsonify({"ok": ok, "info": info, "base": _telegram_webhook_base()})


@admin_bp.route("/admin")
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


@admin_bp.route("/api/admin/points/adjust", methods=["POST"])
@csrf.exempt
@admin_required
def api_admin_points_adjust():
    """管理员手动给指定用户增减积分"""
    data = request.get_json() or {}
    user_id = data.get("user_id")
    amount = int(data.get("amount") or 0)
    remark = str(data.get("remark") or "管理员调整").strip()

    if not user_id or amount == 0:
        return jsonify({"ok": False, "error": "参数无效：需要 user_id 和非 0 的 amount"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        new_bal = add_user_points(cur, int(user_id), "admin_adjust", amount, remark)
        db.commit()
        _log_op(session.get("user_id"), session.get("username"), "points_adjust", f"调整用户 uid={user_id} 积分 {amount:+d}，结余={new_bal}")
        return jsonify({"ok": True, "user_id": user_id, "amount": amount, "balance_after": new_bal})
    finally:
        db.close()


@admin_bp.route("/api/admin/points/logs")
@admin_required
def api_admin_points_logs():
    """管理员查看全站积分变动流水记录"""
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)
    user_id = request.args.get("user_id", type=int)
    action = request.args.get("action", "").strip()

    db = get_db()
    try:
        cur = db.cursor()
        conditions = []
        params = []
        if user_id:
            conditions.append("p.user_id = %s")
            params.append(user_id)
        if action:
            conditions.append("p.action = %s")
            params.append(action)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(f"SELECT COUNT(*) as c FROM point_logs p {where}", params)
        total = cur.fetchone()["c"]

        offset = (page - 1) * per_page
        cur.execute(
            f"""
            SELECT p.id, p.user_id, u.username, u.nickname, p.action, p.change_amount, p.balance_after, p.remark, p.created_at
            FROM point_logs p
            LEFT JOIN users u ON p.user_id = u.id
            {where}
            ORDER BY p.id DESC LIMIT %s OFFSET %s
            """,
            params + [per_page, offset],
        )
        items = cur.fetchall()
        for it in items:
            it["created_at"] = str(it["created_at"]) if it.get("created_at") else ""
        return jsonify({
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        })
    finally:
        db.close()


@admin_bp.route("/api/admin/announcements")
@admin_required
def api_admin_announcements():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id, title, content, type, active, start_time, end_time, created_at, updated_at FROM announcements ORDER BY id DESC")
        items = cur.fetchall()
        for i in items:
            i["start_time"] = str(i["start_time"]) if i.get("start_time") else ""
            i["end_time"] = str(i["end_time"]) if i.get("end_time") else ""
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""
            i["updated_at"] = str(i["updated_at"]) if i.get("updated_at") else ""
        return jsonify({"items": items})
    finally:
        db.close()


@admin_bp.route("/api/admin/announcements", methods=["POST"])
@admin_required
def api_admin_announcements_create():
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO announcements (title, content, type, active, start_time, end_time) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                data.get("title", ""),
                data.get("content", ""),
                data.get("type", "banner"),
                1 if data.get("active", True) else 0,
                data.get("start_time") or None,
                data.get("end_time") or None,
            ),
        )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    finally:
        db.close()


@admin_bp.route("/api/admin/announcements/<int:aid>", methods=["POST"])
@admin_required
def api_admin_announcements_update(aid):
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        sets, vals = [], []
        for k in ("title", "content", "type", "start_time", "end_time"):
            if k in data:
                sets.append(f"{k}=%s")
                vals.append(data[k])
        if "active" in data:
            sets.append("active=%s")
            vals.append(1 if data["active"] else 0)
        if not sets:
            return jsonify({"error": "无有效字段"}), 400
        vals.append(aid)
        cur.execute(f"UPDATE announcements SET {','.join(sets)} WHERE id=%s", vals)
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


@admin_bp.route("/api/admin/announcements/<int:aid>", methods=["DELETE"])
@admin_required
def api_admin_announcements_delete(aid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM announcements WHERE id=%s", (aid,))
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


@admin_bp.route("/api/admin/sources")
@admin_required
def api_admin_sources():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id, name, label, color, active, created_at, updated_at FROM sources ORDER BY id ASC")
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""
            i["updated_at"] = str(i["updated_at"]) if i.get("updated_at") else ""
        return jsonify({"items": items})
    finally:
        db.close()


@admin_bp.route("/api/admin/sources", methods=["POST"])
@admin_required
def api_admin_sources_create():
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO sources (name, label, color, active) VALUES (%s,%s,%s,%s)",
            (
                data.get("name", ""),
                data.get("label", ""),
                data.get("color", "#58a6ff"),
                1 if data.get("active", True) else 0,
            ),
        )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    finally:
        db.close()


@admin_bp.route("/api/admin/sources/<int:sid>", methods=["POST"])
@admin_required
def api_admin_sources_update(sid):
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        sets, vals = [], []
        for k in ("name", "label", "color"):
            if k in data:
                sets.append(f"{k}=%s")
                vals.append(data[k])
        if "active" in data:
            sets.append("active=%s")
            vals.append(1 if data["active"] else 0)
        if not sets:
            return jsonify({"error": "无有效字段"}), 400
        vals.append(sid)
        cur.execute(f"UPDATE sources SET {','.join(sets)} WHERE id=%s", vals)
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


@admin_bp.route("/api/admin/sources/<int:sid>", methods=["DELETE"])
@admin_required
def api_admin_sources_delete(sid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM sources WHERE id=%s", (sid,))
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


@admin_bp.route("/api/admin/pending_imports")
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


@admin_bp.route("/api/admin/confirm_import", methods=["POST"])
@admin_required

def api_confirm_import():
    """管理员确认导入指定关键词"""
    data = request.get_json()
    keywords = data.get("keywords", [])
    if not keywords:
        return jsonify({"error": "请选择关键词"}), 400

    import requests as http_requests
    API_URL = _get_import_api_urls()[0]
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
                app.logger.warning("api_confirm_import failed: %s", e)
                results["details"].append({"keyword": kw, "error": str(e)})
        _log_op(session.get("user_id"), session.get("username"), "import", f"导入{len(keywords)}个关键词")
    finally:
        db.close()
    return jsonify(results)


@admin_bp.route("/api/admin/logs")
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


@admin_bp.route("/api/admin/filter_words")
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


@admin_bp.route("/api/admin/filter_words", methods=["POST"])
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
        _clear_filter_words_cache()
        # 后台异步更新这一个新词的打标，不卡前台响应
        import threading
        def _bg_update_one_word(w):
            try:
                d = get_db()
                c = d.cursor()
                c.execute("UPDATE resources SET is_filtered = 1 WHERE is_filtered = 0 AND (title LIKE %s OR note LIKE %s OR keyword LIKE %s)", (f"%{w}%", f"%{w}%", f"%{w}%"))
                d.commit()
                d.close()
            except Exception as e:
                app.logger.error(f"后台更新过滤词 {w} 失败: {e}")
        threading.Thread(target=_bg_update_one_word, args=(word,)).start()
        return jsonify({"ok": True})
    finally:
        db.close()


@admin_bp.route("/api/admin/filter_words/<int:fid>", methods=["DELETE"])
@admin_required
def api_filter_words_del(fid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM filter_words WHERE id = %s", (fid,))
        db.commit()
        _clear_filter_words_cache()
        # 后台异步重建全量打标
        import threading
        def _bg_rebuild_all():
            try:
                import time
                time.sleep(1) # wait for cache clear
                d = get_db()
                c = d.cursor()
                c.execute("UPDATE resources SET is_filtered = 0")
                words = _get_cached_filter_words()
                for w in words:
                    c.execute("UPDATE resources SET is_filtered = 1 WHERE is_filtered = 0 AND (title LIKE %s OR note LIKE %s OR keyword LIKE %s)", (f"%{w}%", f"%{w}%", f"%{w}%"))
                d.commit()
                d.close()
            except Exception as e:
                app.logger.error(f"后台重建过滤词失败: {e}")
        threading.Thread(target=_bg_rebuild_all).start()
        return jsonify({"ok": True})
    finally:
        db.close()


@admin_bp.route("/api/admin/stats/dashboard")
@admin_required
def api_admin_dashboard():
    """后台统计数据（图表用，带 Redis 缓存加速）"""
    force_refresh = request.args.get("refresh") == "1"
    cache_key = "admin:cache:dashboard"
    r = None
    try:
        r = get_redis()
        if not force_refresh:
            cached = r.get(cache_key)
            if cached:
                return jsonify(json.loads(cached))
    except Exception:
        app.logger.exception("api_admin_dashboard failed")
        pass

    payload = {}
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
        total_visits, today_visits = _get_visit_counts()
        stats["total_visits"] = total_visits
        stats["today_visits"] = today_visits

        payload = {
            "daily_resources": daily_resources,
            "daily_searches": daily_searches,
            "top_searches": top_searches,
            "type_dist": type_dist,
            "source_dist": source_dist,
            "quality_dist": quality_dist,
            "overview": stats,
            "total_visits": total_visits,
            "today_visits": today_visits,
        }
    finally:
        db.close()
    try:
        payload["growth"] = _growth_metrics()
    except Exception as e:
        app.logger.warning("growth metrics: %s", e)
        payload["growth"] = {"error": str(e)[:160]}

    if r:
        try:
            r.setex(cache_key, 300, json.dumps(payload, default=str))
        except Exception as e:
            app.logger.warning("cache dashboard failed: %s", e)

    return jsonify(payload)


@admin_bp.route("/api/admin/settings")
@admin_required
def api_admin_settings():
    """获取所有系统设置（敏感值脱敏）"""
    _ensure_telegram_settings_keys()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT `key`, value, description, updated_at FROM settings ORDER BY `key`")
        items = cur.fetchall()
        secret_keys = {"telegram_bot_token", "admin_pass", "tmdb_api_key", "secret_key"}
        for i in items:
            i["updated_at"] = str(i["updated_at"]) if i.get("updated_at") else ""
            if i.get("key") in secret_keys and i.get("value"):
                i["value_masked"] = _mask_secret(i["value"])
                i["has_value"] = True
                # 前端编辑用：不回传明文；有值时用占位提示
                i["value"] = ""
            else:
                i["has_value"] = bool(i.get("value"))
                i["value_masked"] = ""
        # 附带当前生效的 TG 登录状态（env 兜底也算）
        return jsonify({
            "items": items,
            "telegram_login": {
                "enabled": bool(_telegram_bot_token() and _telegram_bot_username()),
                "bot_username": _telegram_bot_username(),
                "token_configured": bool(_telegram_bot_token()),
                "superadmin_ids": sorted(_telegram_superadmin_ids()),
                "required_chat": _telegram_required_chat(),
                "required_chat_link": (
                    (lambda c: (c if c.startswith("http") else f"https://t.me/{c.lstrip('@')}"))(_telegram_required_chat())
                    if _telegram_required_chat() else ""
                ),
            },
        })
    finally:
        db.close()


@admin_bp.route("/api/admin/settings", methods=["POST"])
@admin_required
def api_admin_settings_update():
    """更新系统设置（upsert）"""
    _ensure_telegram_settings_keys()
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        updated = 0
        settings_list = data.get("settings", [])
        pairs = []
        if settings_list:
            for item in settings_list:
                if isinstance(item, dict) and "key" in item and "value" in item:
                    pairs.append((item["key"], item["value"]))
        else:
            for k, v in data.items():
                if k in ("admin_pass", "settings"):
                    continue
                pairs.append((k, v))

        for k, v in pairs:
            k = str(k).strip()
            if not k:
                continue
            # 敏感字段留空 = 不修改
            if k in ("telegram_bot_token", "admin_pass", "tmdb_api_key") and (v is None or str(v).strip() == ""):
                continue
            if k == "telegram_bot_username":
                v = str(v).strip().lstrip("@")
            cur.execute("SELECT `key` FROM settings WHERE `key`=%s", (k,))
            if cur.fetchone():
                cur.execute("UPDATE settings SET value=%s WHERE `key`=%s", (str(v), k))
            else:
                cur.execute(
                    "INSERT INTO settings (`key`, value, description) VALUES (%s, %s, %s)",
                    (k, str(v), ""),
                )
            updated += 1

        # 可选：单独改管理员密码字段（旧逻辑兼容）
        admin_pass = data.get("admin_pass")
        if admin_pass:
            cur.execute("SELECT `key` FROM settings WHERE `key`='admin_pass'")
            if cur.fetchone():
                cur.execute("UPDATE settings SET value=%s WHERE `key`='admin_pass'", (str(admin_pass),))
            else:
                cur.execute(
                    "INSERT INTO settings (`key`, value, description) VALUES ('admin_pass', %s, '管理员密码')",
                    (str(admin_pass),),
                )
            updated += 1

        db.commit()
        for ck in list(_cache.keys()):
            if "/api/" in ck:
                del _cache[ck]
        return jsonify({"ok": True, "updated": updated})
    finally:
        db.close()


@admin_bp.route("/api/admin/search_logs")
@admin_required
def api_admin_search_logs():
    db = get_db()
    try:
        cur = db.cursor()
        per_page = min(int(request.args.get("per_page", 50)), 200)
        page = int(request.args.get("page", 1))
        offset = (page - 1) * per_page
        cur.execute(
            "SELECT id, keyword, user_id, ip, created_at FROM search_logs ORDER BY id DESC LIMIT %s OFFSET %s",
            (per_page, offset),
        )
        items = cur.fetchall()
        cur.execute("SELECT COUNT(*) as c FROM search_logs")
        total = cur.fetchone()["c"]
        return jsonify({"items": items, "total": total, "page": page, "per_page": per_page})
    finally:
        db.close()


@admin_bp.route("/api/admin/search_logs/<int:sid>", methods=["DELETE"])
@admin_required
def api_admin_search_logs_delete(sid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM search_logs WHERE id=%s", (sid,))
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


@admin_bp.route("/api/admin/search_logs/cleanup", methods=["POST"])
@admin_required
def api_admin_search_logs_cleanup():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT value FROM settings WHERE `key`='search_log_retention_days'")
        row = cur.fetchone()
        days = int(row["value"]) if row and row["value"] else 90
        if days <= 0:
            return jsonify({"ok": True, "deleted": 0, "msg": "永久保留模式，未清理"})
        cur.execute(
            "DELETE FROM search_logs WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (days,),
        )
        db.commit()
        return jsonify({"ok": True, "deleted": cur.rowcount, "retention_days": days})
    finally:
        db.close()


@admin_bp.route("/api/admin/resources/batch", methods=["POST"])
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
            _invalidate_resource_cache()
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
            _invalidate_resource_cache()
            return jsonify({"ok": True, "affected": cur.rowcount})

        elif action == "update_type":
            rtype = data.get("value", "")
            cur.execute(f"UPDATE resources SET type = %s {where}", [rtype] + params)
            db.commit()
            _invalidate_resource_cache()
            return jsonify({"ok": True, "affected": cur.rowcount})

        return jsonify({"error": f"未知操作: {action}"}), 400
    finally:
        db.close()


@admin_bp.route("/api/admin/resources")
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

        link_status = request.args.get("link_status", "").strip()
        if link_status in ("dead", "alive", "unknown"):
            conditions.append("link_status = %s")
            params.append(link_status)
        elif link_status == "unchecked":
            conditions.append("link_status IS NULL AND last_checked IS NULL")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        allowed_sorts = {"created_at", "id", "title", "source", "quality", "type", "click_count"}
        if sort not in allowed_sorts:
            sort = "created_at"
        order_dir = "DESC" if order == "desc" else "ASC"

        cur.execute(f"SELECT COUNT(*) as total FROM resources {where}", params)
        total = cur.fetchone()["total"]

        cur.execute(
            f"SELECT id, title, note, source, url, quality, type, year, keyword, "
            f"click_count, created_at, link_status, last_checked FROM resources {where} "
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


@admin_bp.route("/api/admin/resources/<int:rid>", methods=["POST"])
@admin_required
def api_admin_resource_update(rid):
    """单条资源编辑"""
    data = request.get_json() or {}
    fields = []
    params = []
    for col in ("title", "url", "source", "type", "quality", "year"):
        if col in data:
            fields.append(f"{col} = %s")
            params.append(data[col])
    if not fields:
        return jsonify({"error": "无更新字段"}), 400
    params.append(rid)
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(f"UPDATE resources SET {', '.join(fields)} WHERE id = %s", params)
        db.commit()
        _invalidate_resource_cache()
        _log_op(session.get("user_id"), session.get("username"), "resource_update", f"编辑资源 rid={rid}")
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


@admin_bp.route("/api/admin/duplicates")
@admin_required
def api_admin_duplicates():
    """资源去重：按 URL 或标题分组统计重复（毫秒级高并发子查询优化版）"""
    by = request.args.get("by", "url").strip()
    if by not in ("url", "title", "keyword"):
        by = "url"
    limit = min(int(request.args.get("limit", 50)), 200)
    scan_scope = min(int(request.args.get("scope", 100000)), 300000)
    db = get_db()
    try:
        cur = db.cursor(pymysql.cursors.DictCursor)
        if by == "url":
            cur.execute(
                f"""
                SELECT url as key_val, COUNT(*) as cnt, GROUP_CONCAT(id ORDER BY id DESC) as ids,
                       GROUP_CONCAT(title SEPARATOR ' | ') as titles,
                       GROUP_CONCAT(source SEPARATOR ',') as sources
                FROM (
                    SELECT id, url, title, source FROM resources WHERE url IS NOT NULL AND url != '' ORDER BY id DESC LIMIT %s
                ) sub
                GROUP BY url HAVING cnt > 1 ORDER BY cnt DESC LIMIT %s
                """,
                (scan_scope, limit)
            )
        elif by == "title":
            cur.execute(
                f"""
                SELECT title as key_val, COUNT(*) as cnt, GROUP_CONCAT(id ORDER BY id DESC) as ids,
                       GROUP_CONCAT(url SEPARATOR ' | ') as urls,
                       GROUP_CONCAT(source SEPARATOR ',') as sources
                FROM (
                    SELECT id, title, url, source FROM resources WHERE title IS NOT NULL AND title != '' ORDER BY id DESC LIMIT %s
                ) sub
                GROUP BY title HAVING cnt > 1 ORDER BY cnt DESC LIMIT %s
                """,
                (scan_scope, limit)
            )
        else:
            cur.execute(
                f"""
                SELECT keyword as key_val, COUNT(*) as cnt, GROUP_CONCAT(id ORDER BY id DESC) as ids,
                       GROUP_CONCAT(title SEPARATOR ' | ') as titles,
                       GROUP_CONCAT(source SEPARATOR ',') as sources
                FROM (
                    SELECT id, keyword, title, source FROM resources WHERE keyword IS NOT NULL AND keyword != '' ORDER BY id DESC LIMIT %s
                ) sub
                GROUP BY keyword HAVING cnt > 1 ORDER BY cnt DESC LIMIT %s
                """,
                (scan_scope, limit)
            )
        items = cur.fetchall() or []
        return jsonify({"items": items, "by": by, "total": len(items)})
    finally:
        db.close()


@admin_bp.route("/api/admin/duplicates/clean", methods=["POST"])
@admin_required
def api_admin_duplicates_clean():
    """执行真实资源库物理去重：彻底删除多余记录，极速安全版"""
    data = request.get_json() or {}
    by = data.get("by", "url")
    keep = data.get("keep", "newest")
    group_key = data.get("key")

    import pymysql
    conn = pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        read_timeout=60,
        write_timeout=60,
    )
    try:
        cur = conn.cursor()
        deleted_count = 0
        
        if group_key:
            field = "url" if by == "url" else ("title" if by == "title" else "keyword")
            cur.execute(f"SELECT id FROM resources WHERE {field} = %s ORDER BY id {'DESC' if keep == 'newest' else 'ASC'}", (group_key,))
            rows = cur.fetchall()
            if len(rows) > 1:
                del_ids = [r["id"] for r in rows[1:]]
                for i in range(0, len(del_ids), 200):
                    batch = del_ids[i:i+200]
                    fmt = ','.join(['%s'] * len(batch))
                    cur.execute(f"DELETE FROM resources WHERE id IN ({fmt})", tuple(batch))
                    deleted_count += cur.rowcount
        else:
            order_clause = "ORDER BY id DESC" if keep == "newest" else "ORDER BY id ASC"
            partition_field = "url" if by == "url" else ("title" if by == "title" else "keyword")
            
            cur.execute(f"""
                SELECT r.id 
                FROM (
                    SELECT id, {partition_field},
                           ROW_NUMBER() OVER(PARTITION BY {partition_field} {order_clause}) as rn
                    FROM resources
                    WHERE {partition_field} IS NOT NULL AND {partition_field} != ''
                ) r
                WHERE r.rn > 1
            """)
            rows = cur.fetchall() or []
            del_ids = [row["id"] for row in rows]
            
            if del_ids:
                for i in range(0, len(del_ids), 200):
                    batch = del_ids[i:i+200]
                    fmt = ','.join(['%s'] * len(batch))
                    cur.execute(f"DELETE FROM resources WHERE id IN ({fmt})", tuple(batch))
                    deleted_count += cur.rowcount

        conn.commit()
        _invalidate_resource_cache()
        return jsonify({"ok": True, "deleted_count": deleted_count})
    except Exception as e:
        app.logger.exception("api_admin_duplicates_clean failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@admin_bp.route("/api/admin/import_queue")
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


@admin_bp.route("/api/admin/import_queue/retry", methods=["POST"])
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


@admin_bp.route("/api/admin/import_queue/<int:qid>", methods=["DELETE"])
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


@admin_bp.route("/api/admin/import_queue/clear", methods=["POST"])
@admin_required
def api_admin_import_queue_clear():
    """一键清空所有导入队列"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM import_queue")
        cnt = cur.fetchone()["cnt"]
        cur.execute("DELETE FROM import_queue")
        db.commit()
        return jsonify({"ok": True, "deleted": cnt})
    finally:
        db.close()


@admin_bp.route("/api/admin/process_queue", methods=["POST"])
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
        API_URL = row["value"] if row else "https://pansou.42078207.qzz.io/api/search"
        
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
                app.logger.warning("api_admin_process_queue failed: %s", e)
                cur.execute("UPDATE import_queue SET status='failed', error_msg=%s WHERE id=%s",
                           (str(e)[:200], task["id"]))
                db.commit()
        
        # 清除缓存
        for k in list(_cache.keys()):
            if "/api/" in k:
                del _cache[k]

        # 导入完成后自动检测新导入资源的链接有效性
        _auto_scan_unchecked_bg(50)

        return jsonify({"ok": True, "processed": processed, "total_pending": len(tasks)})
    finally:
        db.close()


@admin_bp.route("/api/admin/logs/clear", methods=["POST"])
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


@admin_bp.route("/api/admin/test_apis")
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
                app.logger.warning("api_admin_test_apis failed: %s", e)
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
                app.logger.warning("api_admin_test_apis failed")
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


@admin_bp.route("/api/admin/deadlinks/scan", methods=["POST"])
@admin_required
def api_admin_deadlinks_scan():
    """扫描失效链接（网盘链接，排除磁力/电驴）- 使用各网盘API精准检测"""
    data = request.get_json() or {}
    source_filter = data.get("source", "")
    limit = min(int(data.get("limit", 50)), 1000)
    page = int(data.get("page", 1))
    skip_checked = data.get("skip_checked", True)  # 默认跳过已检测
    only_unchecked = data.get("only_unchecked", False)  # 只检测未检测的
    rescan_alive = data.get("rescan_alive", False)  # 重新扫描之前正常的

    db = get_db()
    try:
        cur = db.cursor()
        exclude_sources = "('magnet', 'ed2k', 'other')"
        conditions = [f"source NOT IN {exclude_sources} AND url NOT LIKE 'magnet:%' AND url NOT LIKE 'ed2k:%'"]
        params = []

        if source_filter:
            conditions.append("source = %s")
            params.append(source_filter)

        if only_unchecked:
            conditions.append("last_checked IS NULL")
        elif rescan_alive:
            conditions.append("link_status = 'alive' AND last_checked < DATE_SUB(NOW(), INTERVAL 7 DAY)")
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

        # 并发检测：50 个链接串行最坏 500s（每个 10s 超时），gunicorn 30s 就杀请求
        # 用线程池并发，max_workers=20，50 个链接 ~10s 完成
        from concurrent.futures import ThreadPoolExecutor

        def _check_one(item):
            url = item.get("url", "")
            source = (item.get("source") or "").lower()
            if not url:
                return item, "invalid", 0, "空链接"
            elif url.startswith("magnet:") or url.startswith("ed2k:"):
                return item, "skip", 0, ""
            else:
                status, code, msg = _check_single_link(url, source)
                return item, status, code, msg

        checked = []
        with ThreadPoolExecutor(max_workers=20) as pool:
            for item, status, code, msg in pool.map(_check_one, items):
                checked.append((item, status, code, msg))
                if status == "dead":
                    dead_count += 1
                elif status == "alive":
                    alive_count += 1
                elif status not in ("skip",):
                    unknown_count += 1

        for item, status, code, msg in checked:
            url = item.get("url", "")
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
                    app.logger.warning("api_admin_deadlinks_scan failed")
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


@admin_bp.route("/api/admin/deadlinks/list")
@admin_required
def api_admin_deadlinks_list():
    """获取所有被标记为失效(dead)或可疑(suspect)的死链明细列表"""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(max(1, request.args.get("per_page", 20, type=int)), 100)
    source = (request.args.get("source") or "").strip()
    status_filter = (request.args.get("status") or "dead").strip()
    offset = (page - 1) * per_page

    db = get_db()
    try:
        cur = db.cursor(pymysql.cursors.DictCursor)
        conds = []
        params = []
        
        if status_filter == "suspect":
            conds.append("link_status = 'suspect'")
        elif status_filter == "all_dead":
            conds.append("link_status IN ('dead', 'suspect')")
        else:
            conds.append("link_status = 'dead'")

        if source:
            conds.append("source = %s")
            params.append(source)

        where_sql = "WHERE " + " AND ".join(conds) if conds else ""
        cur.execute(f"SELECT COUNT(*) as total FROM resources {where_sql}", params)
        total = cur.fetchone()["total"]

        cur.execute(
            f"""
            SELECT id, title, source, url, quality, type, link_status, last_checked, created_at
            FROM resources {where_sql}
            ORDER BY last_checked DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            params + [per_page, offset],
        )
        items = cur.fetchall() or []
        for i in items:
            i["last_checked"] = str(i["last_checked"]) if i.get("last_checked") else ""
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""

        return jsonify({
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        })
    finally:
        db.close()


@admin_bp.route("/api/admin/deadlinks/delete", methods=["POST"])
@admin_required
def api_admin_deadlinks_delete():
    """批量删除失效链接

    支持两种模式：
    - {"ids": [1,2,3]}        按指定 id 删除（兼容旧调用）
    - {"all": true, "source": "可选"}  按 link_status='dead' 全删（可叠加来源过滤）
    """
    data = request.get_json() or {}
    ids = data.get("ids", [])
    delete_all = data.get("all", False)
    source = (data.get("source") or "").strip()

    if not ids and not delete_all:
        return jsonify({"error": "请选择要删除的链接"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        if delete_all:
            # 先统计要删多少条，用于二次确认/日志
            if source:
                cur.execute("SELECT COUNT(*) c FROM resources WHERE link_status='dead' AND source=%s", (source,))
            else:
                cur.execute("SELECT COUNT(*) c FROM resources WHERE link_status='dead'")
            count = cur.fetchone()["c"]
            if count == 0:
                return jsonify({"ok": True, "deleted": 0, "total": 0})
            if source:
                cur.execute("DELETE FROM resources WHERE link_status='dead' AND source=%s", (source,))
            else:
                cur.execute("DELETE FROM resources WHERE link_status='dead'")
            deleted = cur.rowcount
            db.commit()
            _invalidate_resource_cache()
            try:
                cur.execute(
                    "INSERT INTO operation_logs (action, detail) VALUES (%s, %s)",
                    ("deadlink_delete", f"批量删除死链 {deleted} 条" + (f"（来源:{source}）" if source else ""))
                )
                db.commit()
            except Exception:
                app.logger.exception("api_admin_deadlinks_delete failed")
                pass
            return jsonify({"ok": True, "deleted": deleted, "total": count})

        # 按 ids 删除
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", ids)
        deleted = cur.rowcount
        db.commit()
        _invalidate_resource_cache()
        try:
            cur.execute(
                "INSERT INTO operation_logs (action, detail) VALUES (%s, %s)",
                ("deadlink_delete", f"删除死链 {deleted} 条（指定 id）")
            )
            db.commit()
        except Exception:
            app.logger.exception("api_admin_deadlinks_delete failed")
            pass
        return jsonify({"ok": True, "deleted": deleted})
    finally:
        db.close()


@admin_bp.route("/api/admin/deadlinks/sources")
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


@admin_bp.route("/api/admin/deadlinks/unchecked_count")
@admin_required
def api_admin_deadlinks_unchecked_count():
    """获取未检测链接数量（带 Redis 缓存加速）"""
    force_refresh = request.args.get("refresh") == "1"
    cache_key = "admin:cache:unchecked_count"
    r = None
    try:
        r = get_redis()
        if not force_refresh:
            cached = r.get(cache_key)
            if cached:
                return jsonify(json.loads(cached))
    except Exception:
        app.logger.exception("api_admin_deadlinks_unchecked_count failed")
        pass

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
        payload = {"items": items, "total": total}
        if r:
            try:
                r.setex(cache_key, 300, json.dumps(payload))
            except Exception:
                app.logger.exception("api_admin_deadlinks_unchecked_count failed")
                pass
        return jsonify(payload)
    finally:
        db.close()


@admin_bp.route("/api/admin/deadlinks/auto_scan", methods=["POST"])
@admin_required
def api_admin_deadlinks_auto_scan():
    """一键自动检测：批量扫描+标记+删除失效链接"""
    import threading

    data = request.get_json() or {}
    batch_size = min(int(data.get("batch_size", 100)), 500)
    max_batches = min(int(data.get("max_batches", 10)), 500)  # 最多500轮
    auto_delete = data.get("auto_delete", False)  # 自动删除失效链接

    def _auto_scan_worker(batch_size, max_batches, auto_delete):
        total_scanned = 0
        total_dead = 0
        total_alive = 0

        for batch_num in range(max_batches):
            try:
                db = get_db()
                cur = db.cursor()
                # 取未检测的链接
                cur.execute(
                    "SELECT id, title, source, url, password FROM resources "
                    "WHERE source NOT IN ('magnet', 'ed2k', 'other') AND last_checked IS NULL "
                    "ORDER BY id LIMIT %s", (batch_size,)
                )
                items = cur.fetchall()
                db.close()

                if not items:
                    break  # 全部检测完毕

                # 逐条检测
                dead_ids = []
                for item in items:
                    url = item.get("url", "")
                    source = (item.get("source") or "").lower()
                    status, code, msg = _check_single_link(url, source)

                    # 标记
                    try:
                        db = get_db()
                        cur = db.cursor()
                        cur.execute(
                            "UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s",
                            (status, item["id"])
                        )
                        db.commit()
                        db.close()
                    except:
                        app.logger.warning("_auto_scan_worker failed")
                        pass

                    if status == "dead":
                        total_dead += 1
                        dead_ids.append(item["id"])
                    elif status == "alive":
                        total_alive += 1
                    total_scanned += 1

                # 自动删除失效链接
                if auto_delete and dead_ids:
                    try:
                        db = get_db()
                        cur = db.cursor()
                        placeholders = ",".join(["%s"] * len(dead_ids))
                        cur.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", dead_ids)
                        db.commit()
                        db.close()
                        _invalidate_resource_cache()
                    except:
                        app.logger.warning("_auto_scan_worker failed")
                        pass

                # 记录日志
                try:
                    db = get_db()
                    cur = db.cursor()
                    cur.execute(
                        "INSERT INTO operation_logs (action, detail) VALUES (%s, %s)",
                        ("auto_scan", f"批次{batch_num+1}: 扫描{len(items)}条, 存活{len(items)-len(dead_ids)}, 失效{len(dead_ids)}")
                    )
                    db.commit()
                    db.close()
                except:
                    app.logger.warning("_auto_scan_worker failed")
                    pass

            except Exception as e:
                app.logger.warning("_auto_scan_worker failed: %s", e)
                pass

        # 最终日志
        try:
            db = get_db()
            cur = db.cursor()
            cur.execute(
                "INSERT INTO operation_logs (action, detail) VALUES (%s, %s)",
                ("auto_scan_done", f"完成: 共扫描{total_scanned}条, 存活{total_alive}, 失效{total_dead}" + (f", 已删除{total_dead}" if auto_delete else ""))
            )
            db.commit()
            db.close()
        except:
            app.logger.exception("_auto_scan_worker failed")
            pass

    # 启动后台线程
    t = threading.Thread(target=_auto_scan_worker, args=(batch_size, max_batches, auto_delete), daemon=True)
    t.start()

    return jsonify({
        "ok": True,
        "message": f"自动检测已启动：每批{batch_size}条, 最多{max_batches}批",
        "auto_delete": auto_delete
    })


@admin_bp.route("/api/admin/cleaner/preview")
@admin_required
def api_admin_cleaner_preview():
    limit = request.args.get("limit", 20, type=int)
    db = get_db()
    try:
        from services import cleaner
        cur = db.cursor()
        preview = cleaner.preview_clean_batch(cur, limit=limit)
        return jsonify(preview)
    except Exception as e:
        app.logger.exception("api_admin_cleaner_preview failed")
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@admin_bp.route("/api/admin/cleaner/run", methods=["POST"])
@admin_required
@csrf.exempt
def api_admin_cleaner_run():
    data = request.get_json(silent=True) or {}
    batch_size = int(data.get("batch_size") or 1000)
    db = get_db()
    try:
        from services import cleaner
        cur = db.cursor()
        result = cleaner.run_clean_batch(cur, batch_size=batch_size)
        db.commit()
        return jsonify(result)
    except Exception as e:
        app.logger.exception("api_admin_cleaner_run failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


@admin_bp.route("/api/admin/dedup/rebuild", methods=["POST"])
@admin_required
@csrf.exempt
def api_admin_dedup_rebuild():
    data = request.get_json(silent=True) or {}
    only_multi = data.get("only_multi", True)
    max_groups = int(data.get("max_groups") or 5000)
    db = get_db()
    try:
        from services import dedup as dedup_svc
        cur = db.cursor()
        result = dedup_svc.rebuild_aggregate(cur, only_multi=bool(only_multi), max_groups=max_groups)
        db.commit()
        try:
            cur.execute(
                "INSERT INTO operation_logs (action, detail) VALUES (%s, %s)",
                ("dedup_rebuild", json.dumps(result, ensure_ascii=False)[:500]),
            )
            db.commit()
        except Exception:
            app.logger.exception("api_admin_dedup_rebuild failed")
            pass
        return jsonify(result)
    except Exception as e:
        app.logger.exception("api_admin_dedup_rebuild failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


@admin_bp.route("/api/admin/deadlinks/auto_clean/preview")
@admin_required
def api_admin_auto_clean_preview():
    days = request.args.get("days", 7, type=int)
    db = get_db()
    try:
        from services import auto_clean as ac
        cur = db.cursor()
        preview = ac.preview_auto_clean(cur, days=days)
        stats = ac.scan_invalid_stats(cur)
        preview["stats"] = stats
        return jsonify(preview)
    except Exception as e:
        app.logger.exception("api_admin_auto_clean_preview failed")
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


@admin_bp.route("/api/admin/deadlinks/auto_clean", methods=["POST"])
@admin_required
@csrf.exempt
def api_admin_auto_clean_run():
    data = request.get_json(silent=True) or {}
    days = int(data.get("days") or 7)
    limit = int(data.get("limit") or 5000)
    db = get_db()
    try:
        from services import auto_clean as ac
        cur = db.cursor()
        result = ac.run_auto_clean(cur, days=days, limit=limit)
        db.commit()
        if result.get("deleted"):
            _invalidate_resource_cache()
        try:
            cur.execute(
                "INSERT INTO operation_logs (action, detail) VALUES (%s, %s)",
                ("auto_clean", f"days={days} deleted={result.get('deleted')}"),
            )
            db.commit()
        except Exception:
            app.logger.exception("api_admin_auto_clean_run failed")
            pass
        return jsonify(result)
    except Exception as e:
        app.logger.exception("api_admin_auto_clean_run failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


@admin_bp.route("/api/admin/sub_push/check", methods=["POST"])
@admin_required
@csrf.exempt
def api_admin_sub_push_check():
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run"))
    since_hours = data.get("since_hours")
    since_hours = int(since_hours) if since_hours is not None else None
    db = get_db()
    try:
        from services import sub_push as sp
        cur = db.cursor()
        hits = sp.find_new_for_subscriptions(cur, since_hours=since_hours)
        if dry_run:
            return jsonify({
                "ok": True,
                "dry_run": True,
                "hits": len(hits),
                "preview": [
                    {
                        "user_id": h["user_id"],
                        "keyword": h["keyword"],
                        "telegram_id": h.get("telegram_id"),
                        "count": len(h["resources"]),
                    }
                    for h in hits
                ],
            })

        def _send_tg(chat_id, text):
            _telegram_api("sendMessage", {"chat_id": chat_id, "text": text})

        def _mk_ann(title, content):
            cur.execute(
                "INSERT INTO announcements (title, content, type, active) VALUES (%s,%s,%s,1)",
                (title[:200], content[:2000], "info"),
            )

        result = sp.push_hits(hits, send_telegram=_send_tg, create_announcement=_mk_ann)
        db.commit()
        return jsonify(result)
    except Exception as e:
        app.logger.exception("api_admin_sub_push_check failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


@admin_bp.route("/api/admin/deadlinks/bg_start", methods=["POST"])
@admin_required
def api_admin_deadlinks_bg_start():
    data = request.get_json() or {}
    source_filter = (data.get("source") or "").strip()
    rescan_alive = bool(data.get("rescan_alive", True))
    stale_days = int(data.get("stale_days") or 3)
    force_local = bool(data.get("force_local", False))

    if not force_local:
        try:
            from services.deadlink_worker import start_job
            from services.deadlink_queue import get_state
            st = get_state()
            if st.get("running") and (
                st.get("jobs", 0) > 0
                or st.get("results", 0) > 0
                or st.get("pending", 0) > 0
                or st.get("extracting")
            ):
                return jsonify({"ok": False, "msg": "集群流水线任务已在运行中", "state": st, "mode": "cluster"})
            st = start_job(source_filter=source_filter, rescan_alive=rescan_alive, stale_days=stale_days)
            online = _cluster_workers_online()
            tip = (
                (st.get("msg") or "已投递 Celery 死链任务（Python 消息队列）")
                + f"；worker 登记={online}。"
                + (
                    "请确认 deadlink-celery-extract/check/write 已启动。"
                    if online <= 0
                    else "Celery：extract→check→write。"
                )
            )
            return jsonify({"ok": True, "msg": tip, "state": st, "mode": "celery", "workers_online": online})
        except Exception as e:
            app.logger.warning("cluster deadlink start failed, fallback local: %s", e)

    if _bg_scan_state["running"]:
        return jsonify({"ok": False, "msg": "本地巡检任务已在运行中", "state": _bg_scan_state, "mode": "local"})
    t = threading.Thread(
        target=_bg_scan_worker_thread,
        args=(source_filter, 1000, rescan_alive, stale_days),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "msg": "本地线程池巡检已启动（无集群 worker 时的兜底）", "state": _bg_scan_state, "mode": "local"})


@admin_bp.route("/api/admin/deadlinks/bg_stop", methods=["POST"])
@admin_required
def api_admin_deadlinks_bg_stop():
    _bg_scan_stop_event.set()
    _bg_scan_state["running"] = False
    try:
        from services.deadlink_tasks import stop_job
        st = stop_job()
        return jsonify({"ok": True, "msg": "已停止 Celery 死链任务并清空队列", "state": st})
    except Exception:
        app.logger.exception("api_admin_deadlinks_bg_stop failed")
        try:
            from services.deadlink_queue import request_stop, set_running, get_state
            request_stop()
            set_running(False)
            return jsonify({"ok": True, "msg": "已发送停止信号", "state": get_state()})
        except Exception:
            app.logger.exception("api_admin_deadlinks_bg_stop failed")
            return jsonify({"ok": True, "msg": "已发送停止巡检信号", "state": _bg_scan_state})


@admin_bp.route("/api/admin/deadlinks/bg_status")
@admin_required
def api_admin_deadlinks_bg_status():
    try:
        from services.deadlink_queue import get_state
        st = get_state()
        if (
            st.get("running")
            or st.get("jobs", 0)
            or st.get("results", 0)
            or st.get("queue_len", 0)
            or st.get("celery_check", 0)
            or st.get("celery_write", 0)
            or st.get("workers", 0)
        ):
            return jsonify({"ok": True, "state": st, "mode": "celery"})
    except Exception:
        app.logger.exception("api_admin_deadlinks_bg_status failed")
        pass
    if _bg_scan_state["running"]:
        _bg_scan_state["elapsed_s"] = int(time.time() - _bg_scan_state["start_time"])
    return jsonify({"ok": True, "state": _bg_scan_state, "mode": "local"})


@admin_bp.route("/api/admin/resources/<int:rid>/check", methods=["POST"])
@admin_required
def api_admin_resource_check_one(rid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id, url, source FROM resources WHERE id=%s", (rid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "资源不存在"}), 404
        status, code, msg = _check_single_link(row["url"], (row.get("source") or "").lower())
        cur.execute("UPDATE resources SET link_status=%s, last_checked=NOW() WHERE id=%s", (status, rid))
        db.commit()
        _invalidate_resource_cache()
        return jsonify({"ok": True, "id": rid, "link_status": status, "code": code, "msg": msg})
    finally:
        db.close()


@admin_bp.route("/api/admin/system/flush_cache", methods=["POST"])
@admin_required
def api_admin_system_flush_cache():
    """清空应用缓存。

    修复历史 bug：原实现引用了未定义的 `redis_client`（全局变量实际名为
    `_redis_client`），该分支永远抛 NameError，被外层 except 吞掉后返回泛化错误，
    功能实际从未生效。同时原来的 `flushdb()` 会清空整个 Redis db0 —— 而 db0 与
    Celery broker 共用，flushdb 会连任务队列一起清掉，掀翻死链流水线。

    现改为：只按前缀精确删除本应用的缓存键，绝不触碰 Celery / 访问统计等其他键。
    """
    _APP_CACHE_PREFIXES = (
        "res_web:resources:",
        "res_web:rcount:",
        "res_web:stats:",
        "res_web:keywords:",
        "res_web:hot_searches:",
        "res_web:img:",
        "res_web:ip_geo:",
    )
    try:
        _invalidate_resource_cache()
        removed = 0
        r = get_redis()
        if r is not None:
            for prefix in _APP_CACHE_PREFIXES:
                for key in r.scan_iter(match=prefix + "*", count=500):
                    r.delete(key)
                    removed += 1
        return jsonify({
            "ok": True,
            "msg": f"应用缓存已清空（{removed} 个 Redis 键）",
            "removed": removed,
            "note": "仅清理本应用缓存前缀，Celery 队列与访问统计未受影响",
        })
    except Exception as e:
        app.logger.exception("flush_cache failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@admin_bp.route("/api/admin/tasks/center")
@admin_required
def api_admin_tasks_center():
    """获取所有后台异步任务状态与 Celery 状态"""
    try:
        from services.task_center import get_task_center_state
        r = get_redis()
        st = get_task_center_state(r)
        return jsonify({"ok": True, "data": st})
    except Exception as e:
        app.logger.error("get_task_center_state failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@admin_bp.route("/api/admin/resources/batch_check_async", methods=["POST"])
@csrf.exempt
@admin_required
def api_admin_resources_batch_check_async():
    """将批量资源测活任务提交至后台异步队列（瞬间返回，不阻塞UI）"""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    if not ids or not isinstance(ids, list):
        return jsonify({"ok": False, "msg": "未提供有效的资源 ID 列表"}), 400

    try:
        from services.task_center import start_batch_check_async
        r = get_redis()
        res = start_batch_check_async(ids, r, get_db)
        return jsonify(res)
    except Exception as e:
        app.logger.error("start_batch_check_async failed: %s", e)
        return jsonify({"ok": False, "msg": str(e)}), 500


@admin_bp.route("/api/admin/tasks/stop", methods=["POST"])
@csrf.exempt
@admin_required
def api_admin_tasks_stop():
    """停止后台任务"""
    data = request.get_json() or {}
    task_type = (data.get("type") or "").strip()
    try:
        from services.task_center import stop_task
        r = get_redis()
        res = stop_task(task_type, r)
        return jsonify(res)
    except Exception as e:
        app.logger.exception("api_admin_tasks_stop failed")
        return jsonify({"ok": False, "msg": str(e)}), 500

# 自动载入站长实质性功能增强路由
from . import admin_features

