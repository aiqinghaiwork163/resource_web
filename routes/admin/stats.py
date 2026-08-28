"""Admin dashboard statistics."""
from flask import jsonify

from models.database import get_db
from utils.auth import admin_required

from routes.admin.misc import admin_bp
@admin_bp.route("/api/admin/stats/dashboard")
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
