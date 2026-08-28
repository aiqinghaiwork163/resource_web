"""Miscellaneous admin API routes."""
import json
import time

import requests as http_requests
from flask import Blueprint, jsonify, request, session

from models.database import get_db
from services.operation_log import log_op
from utils.auth import admin_required

admin_bp = Blueprint("admin", __name__)
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
        log_op(session.get("user_id"), session.get("username"), "import", f"导入{len(keywords)}个关键词")
    finally:
        db.close()
    return jsonify(results)

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
