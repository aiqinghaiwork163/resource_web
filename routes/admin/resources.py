"""Admin resource management."""
from flask import jsonify, request

from models.database import get_db
from utils.auth import admin_required
from utils.redis_cache import invalidate_resource_cache

from routes.admin.misc import admin_bp
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
            invalidate_resource_cache()
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
            invalidate_resource_cache()
            return jsonify({"ok": True, "affected": cur.rowcount})

        elif action == "update_type":
            rtype = data.get("value", "")
            cur.execute(f"UPDATE resources SET type = %s {where}", [rtype] + params)
            db.commit()
            invalidate_resource_cache()
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
