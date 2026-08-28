"""Admin system settings."""
from flask import jsonify, request

from models.database import get_db
from utils.auth import admin_required
from utils.memory_cache import invalidate_url_cache_keys

from routes.admin.misc import admin_bp
@admin_bp.route("/api/admin/settings")
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

@admin_bp.route("/api/admin/settings", methods=["POST"])
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
                del memory_cache[k]
        return jsonify({"ok": True, "updated": updated})
    finally:
        db.close()
