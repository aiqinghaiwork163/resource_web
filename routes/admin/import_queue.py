"""Admin import queue management."""
import requests as http_requests

from flask import jsonify, request

from models.database import get_db
from services.import_service import auto_scan_unchecked_bg
from utils.auth import admin_required
from utils.memory_cache import invalidate_url_cache_keys

from routes.admin.misc import admin_bp
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
                del memory_cache[k]

        # 导入完成后自动检测新导入资源的链接有效性
        auto_scan_unchecked_bg(50)

        return jsonify({"ok": True, "processed": processed, "total_pending": len(tasks)})
    finally:
        db.close()
