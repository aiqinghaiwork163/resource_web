"""Admin dead link scanning."""
import threading

from flask import jsonify, request

from models.database import get_db
from services.link_checker import check_single_link
from utils.auth import admin_required
from utils.redis_cache import invalidate_resource_cache

from routes.admin.misc import admin_bp
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
        conditions = [f"source NOT IN {exclude_sources}"]
        params = []

        if source_filter:
            conditions.append("source = %s")
            params.append(source_filter)

        if only_unchecked:
            conditions.append("last_checked IS NULL")
        elif rescan_alive:
            conditions.append("link_status = 'alive' AND last_checked < DATE_SUB(NOW(), INTERVAL 7 DAY))")
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

        for item in items:
            url = item.get("url", "")
            source = (item.get("source") or "").lower()

            if not url:
                status, code, msg = "invalid", 0, "空链接"
                unknown_count += 1
            elif url.startswith("magnet:") or url.startswith("ed2k:"):
                status, code, msg = "skip", 0, ""
            else:
                status, code, msg = check_single_link(url, source)
                if status == "dead":
                    dead_count += 1
                elif status == "alive":
                    alive_count += 1
                else:
                    unknown_count += 1

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

@admin_bp.route("/api/admin/deadlinks/delete", methods=["POST"])
@admin_required
def api_admin_deadlinks_delete():
    """批量删除失效链接"""
    data = request.get_json() or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "请选择要删除的链接"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", ids)
        db.commit()
        invalidate_resource_cache()
        return jsonify({"ok": True, "deleted": cur.rowcount})
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
    """获取未检测链接数量"""
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
        return jsonify({"items": items, "total": total})
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
                    status, code, msg = check_single_link(url, source)

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
                        invalidate_resource_cache()
                    except:
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
                    pass

            except Exception as e:
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
            pass

    # 启动后台线程
    t = threading.Thread(target=_auto_scan_worker, args=(batch_size, max_batches, auto_delete), daemon=True)
    t.start()

    return jsonify({
        "ok": True,
        "message": f"自动检测已启动：每批{batch_size}条, 最多{max_batches}批",
        "auto_delete": auto_delete
    })
