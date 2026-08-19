"""VaultDrive 专用 API — 首页 Feed、片名详情、在线搜索状态等"""
import json
import os
import time

from flask import jsonify, request, session

SOURCE_LABELS = {
    "quark": "夸克", "baidu": "百度", "aliyun": "阿里", "xunlei": "迅雷",
    "tianyi": "天翼", "115": "115", "uc": "UC", "pikpak": "PikPak",
    "magnet": "磁力", "ed2k": "电驴", "mobile": "移动", "123": "123", "others": "其他",
}


def _label(source):
    s = (source or "").lower()
    return SOURCE_LABELS.get(s, source or "")


def _poster_from_images(images):
    if not images:
        return ""
    try:
        arr = json.loads(images) if isinstance(images, str) else images
        return arr[0] if arr else ""
    except Exception:
        return images if str(images).startswith("http") else ""


def _row_to_feed_item(row):
    title = row.get("title") or row.get("keyword") or row.get("note") or ""
    kw = row.get("keyword") or title
    return {
        "id": row.get("id"),
        "title": title,
        "display_title": title.split("/")[0].strip() if title else "",
        "hot_keyword": kw.split("/")[0].strip() if kw else title,
        "keyword": kw,
        "source": row.get("source") or "",
        "source_label": _label(row.get("source")),
        "pan": row.get("source") or "",
        "url": row.get("url") or "",
        "quality": row.get("quality") or "",
        "type": row.get("type") or "",
        "year": row.get("year"),
        "rating": row.get("rating"),
        "poster": row.get("poster") or _poster_from_images(row.get("images")),
        "images": row.get("images"),
        "created_at": str(row.get("created_at") or ""),
        "click_count": row.get("click_count") or 0,
        "is_valid": 1,
        "link_status": None,
    }


def register_vault_routes(app, get_db, auto_import_fn=None):
    """在 Flask app 上注册 VaultDrive 路由"""

    @app.route("/api/home/feed")
    def api_home_feed():
        limit = min(int(request.args.get("limit", 18)), 30)
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT keyword, COUNT(*) as count FROM search_logs "
                "GROUP BY keyword ORDER BY count DESC LIMIT 10"
            )
            hot = [{"keyword": r["keyword"], "count": r["count"]} for r in cur.fetchall()]

            cur.execute(
                "SELECT id, keyword, source, url, title, note, quality, type, year, "
                "rating, images, created_at FROM resources "
                "ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            latest = [_row_to_feed_item(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT sl.keyword, COUNT(*) as cnt FROM search_logs sl "
                "WHERE sl.created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) "
                "GROUP BY sl.keyword ORDER BY cnt DESC LIMIT 5"
            )
            hot_kws = [r["keyword"] for r in cur.fetchall()]
            clicks = []
            if hot_kws:
                placeholders = ",".join(["%s"] * len(hot_kws))
                cur.execute(
                    f"SELECT id, keyword, source, url, title, note, quality, type, year, "
                    f"rating, images, created_at FROM resources "
                    f"WHERE keyword IN ({placeholders}) "
                    f"ORDER BY created_at DESC LIMIT %s",
                    hot_kws + [limit],
                )
                clicks = [_row_to_feed_item(r) for r in cur.fetchall()]

            return jsonify({"hot": hot, "latest": latest, "clicks": clicks})
        finally:
            db.close()

    @app.route("/api/home/personal")
    def api_home_personal():
        uid = session.get("user_id")
        if not uid:
            return jsonify({
                "login": False, "favorites_count": 0,
                "subscription_new_total": 0, "subscriptions": [], "watchlist_ready": [],
            })
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT COUNT(*) as c FROM favorites WHERE user_id = %s", (uid,))
            fav_count = cur.fetchone()["c"]
            cur.execute(
                "SELECT id, keyword, created_at FROM subscriptions WHERE user_id = %s",
                (uid,),
            )
            subs = cur.fetchall()
            return jsonify({
                "login": True,
                "favorites_count": fav_count,
                "subscription_new_total": 0,
                "subscriptions": subs,
                "watchlist_ready": [],
            })
        finally:
            db.close()

    @app.route("/api/home/posters", methods=["POST"])
    def api_home_posters():
        data = request.get_json() or {}
        titles = data.get("titles") or []
        if not titles:
            return jsonify({"items": {}})
        db = get_db()
        try:
            cur = db.cursor()
            result = {}
            for t in titles[:24]:
                t = (t or "").strip()
                if not t:
                    continue
                cur.execute(
                    "SELECT images FROM resources WHERE (title LIKE %s OR keyword LIKE %s) "
                    "AND images IS NOT NULL AND images != '' LIMIT 1",
                    (f"%{t}%", f"%{t}%"),
                )
                row = cur.fetchone()
                if row:
                    p = _poster_from_images(row.get("images"))
                    if p:
                        result[t] = p
            return jsonify({"items": result})
        finally:
            db.close()

    @app.route("/api/home/availability", methods=["POST"])
    def api_home_availability():
        if not session.get("user_id"):
            return jsonify({"items": {}}), 401
        data = request.get_json() or {}
        titles = [t.get("title") for t in (data.get("titles") or []) if t]
        if not titles and isinstance(data.get("titles"), list):
            titles = [x for x in data["titles"] if isinstance(x, str)]
        db = get_db()
        try:
            cur = db.cursor()
            items = {}
            for title in titles[:30]:
                title = (title or "").strip()
                if not title:
                    continue
                cur.execute(
                    "SELECT source, COUNT(*) as cnt FROM resources "
                    "WHERE title LIKE %s OR keyword LIKE %s GROUP BY source",
                    (f"%{title}%", f"%{title}%"),
                )
                rows = cur.fetchall()
                if not rows:
                    items[title] = {"status": "none", "count": 0, "source_labels": []}
                else:
                    total = sum(r["cnt"] for r in rows)
                    labels = [_label(r["source"]) for r in rows[:3]]
                    items[title] = {
                        "status": "live", "count": total, "live": total, "dead": 0,
                        "source_labels": labels,
                    }
            return jsonify({"items": items})
        finally:
            db.close()

    @app.route("/api/home/title_detail")
    def api_home_title_detail():
        title = request.args.get("title", "").strip()
        if not title:
            return jsonify({"error": "缺少 title"}), 400
        db = get_db()
        try:
            cur = db.cursor()
            like = f"%{title}%"
            cur.execute(
                "SELECT id, keyword, source, url, title, note, password, quality, type, "
                "year, rating, images, created_at FROM resources "
                "WHERE title LIKE %s OR keyword LIKE %s OR note LIKE %s "
                "ORDER BY created_at DESC LIMIT 40",
                (like, like, like),
            )
            rows = cur.fetchall()
            items = []
            src_counts = {}
            for r in rows:
                src = r.get("source") or ""
                src_counts[src] = src_counts.get(src, 0) + 1
                items.append({
                    "id": r["id"],
                    "title": r.get("title") or r.get("note") or title,
                    "source": src,
                    "source_label": _label(src),
                    "url": r.get("url") or "",
                    "password": r.get("password") or "",
                    "quality": r.get("quality") or "",
                    "type": r.get("type") or "",
                    "year": r.get("year"),
                    "size": "",
                    "link_status": "live",
                    "is_valid": 1,
                })
            sources = [{"label": _label(k), "source": k, "count": v} for k, v in
                       sorted(src_counts.items(), key=lambda x: -x[1])]
            live = len(items)
            return jsonify({
                "title": title,
                "count": len(items),
                "items": items,
                "sources": sources,
                "avail": {
                    "status": "live" if live else "none",
                    "count": live, "live": live, "dead": 0,
                    "has_live": live > 0, "has_dead": False,
                    "source_labels": [s["label"] for s in sources[:5]],
                    "sources": list(src_counts.keys()),
                },
            })
        finally:
            db.close()

    @app.route("/api/online_status")
    def api_online_status():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify({"status": "idle", "imported": 0})
        status = "done"
        imported = 0
        if auto_import_fn:
            try:
                result = auto_import_fn(q)
                if result is True:
                    status = "pending"
                elif result == "exhausted":
                    status = "exhausted"
            except Exception:
                pass
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT COUNT(*) as c FROM resources WHERE title LIKE %s OR keyword LIKE %s",
                (f"%{q}%", f"%{q}%"),
            )
            imported = cur.fetchone()["c"]
        finally:
            db.close()
        return jsonify({"status": status, "imported": imported})

    @app.route("/api/visit", methods=["POST"])
    def api_visit():
        return jsonify({"ok": True})

    @app.route("/api/announcements")
    def api_announcements():
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT `key`, `value` FROM settings WHERE `key` IN "
                "('announcement_title', 'announcement_content', 'announcement_type', 'announcement_id')"
            )
            rows = {r["key"]: r["value"] for r in cur.fetchall()}
            if rows.get("announcement_content"):
                return jsonify({
                    "id": rows.get("announcement_id", "1"),
                    "title": rows.get("announcement_title", ""),
                    "content": rows.get("announcement_content", ""),
                    "type": rows.get("announcement_type", "info"),
                })
            return jsonify({})
        except Exception:
            return jsonify({})
        finally:
            db.close()

    @app.route("/api/watchlist")
    def api_watchlist_get():
        uid = session.get("user_id")
        if not uid:
            return jsonify({"items": []})
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT w.resource_id, w.status, r.title, r.type, r.year "
                "FROM watch_status w LEFT JOIN resources r ON r.id = w.resource_id "
                "WHERE w.user_id = %s AND w.status = 'want' ORDER BY w.updated_at DESC",
                (uid,),
            )
            return jsonify({"items": cur.fetchall()})
        except Exception:
            return jsonify({"items": []})
        finally:
            db.close()

    @app.route("/api/watchlist", methods=["POST"])
    def api_watchlist_add():
        uid = session.get("user_id")
        if not uid:
            return jsonify({"error": "请先登录"}), 401
        data = request.get_json() or {}
        tmdb_id = data.get("tmdb_id")
        media_type = data.get("media_type", "movie")
        title = data.get("title", "")
        db = get_db()
        try:
            cur = db.cursor()
            rid = data.get("resource_id")
            if not rid and title:
                cur.execute(
                    "SELECT id FROM resources WHERE title LIKE %s OR keyword LIKE %s LIMIT 1",
                    (f"%{title}%", f"%{title}%"),
                )
                row = cur.fetchone()
                rid = row["id"] if row else None
            if rid:
                cur.execute(
                    "INSERT INTO watch_status (user_id, resource_id, status) "
                    "VALUES (%s, %s, 'want') ON DUPLICATE KEY UPDATE status='want'",
                    (uid, rid),
                )
                db.commit()
            return jsonify({"ok": True, "tmdb_id": tmdb_id, "media_type": media_type})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            db.close()

    @app.route("/api/watchlist/<int:tmdb_id>", methods=["DELETE"])
    def api_watchlist_del(tmdb_id):
        uid = session.get("user_id")
        if not uid:
            return jsonify({"error": "未登录"}), 401
        return jsonify({"ok": True})
