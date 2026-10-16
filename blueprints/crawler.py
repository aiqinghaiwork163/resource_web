"""crawler blueprint (split from app.py)."""


from flask import Blueprint


from flask import Flask, jsonify, request, send_from_directory, session, redirect


import json


from .common import _multi_crawler_status, _omdb_crawler_status, _tmdb_crawler_status, admin_required, app


crawler_bp = Blueprint('crawler', __name__)


@crawler_bp.route("/api/admin/tmdb/crawl", methods=["POST"])
@admin_required
def api_tmdb_crawl():
    """启动 TMDB 爬虫（subprocess）"""
    if _tmdb_crawler_status["running"]:
        return jsonify({"ok": False, "error": "爬虫正在运行中"})
    _tmdb_crawler_status.update({"running": True, "progress": 0, "total": 0, "imported": 0, "log": []})
    import subprocess
    subprocess.Popen(
        ["python3", "/app/tmdb_crawler_web.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return jsonify({"ok": True, "message": "爬虫已启动"})


@crawler_bp.route("/api/admin/tmdb/status")
@admin_required
def api_tmdb_status():
    """获取 TMDB 爬虫状态（从 JSON 文件读取）"""
    try:
        with open("/app/data/tmdb_status.json", "r") as f:
            return jsonify(json.load(f))
    except:
        app.logger.exception("api_tmdb_status failed")
        return jsonify(_tmdb_crawler_status)


@crawler_bp.route("/api/admin/multi-crawl", methods=["POST"])
@admin_required
def api_multi_crawl():
    """启动多源爬虫（TVmaze + Trakt + 豆瓣 + TMDB）"""
    if _multi_crawler_status["running"]:
        return jsonify({"ok": False, "error": "多源爬虫正在运行中"})
    _multi_crawler_status.update({"running": True, "phase": "启动中", "progress": 0, "total": 0, "imported": 0, "log": []})
    with open("/app/data/multi_source_status.json", "w") as f:
        json.dump(_multi_crawler_status, f)
    import subprocess
    subprocess.Popen(
        ["python3", "-u", "/app/multi_source_crawler_web.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return jsonify({"ok": True, "message": "多源爬虫已启动（TVmaze+Trakt+豆瓣+TMDB）"})


@crawler_bp.route("/api/admin/multi-crawl/status")
@admin_required
def api_multi_crawl_status():
    """获取多源爬虫状态"""
    try:
        with open("/app/data/multi_source_status.json", "r") as f:
            return jsonify(json.load(f))
    except:
        app.logger.exception("api_multi_crawl_status failed")
        return jsonify(_multi_crawler_status)


@crawler_bp.route("/api/admin/omdb/crawl", methods=["POST"])
@admin_required
def api_omdb_crawl():
    """启动OMDb评分爬虫（IMDb+烂番茄+Metacritic）"""
    if _omdb_crawler_status["running"]:
        return jsonify({"ok": False, "error": "评分爬虫正在运行中"})
    _omdb_crawler_status.update({"running": True, "progress": 0, "total": 0, "success": 0, "failed": 0, "log": []})
    with open("/app/data/omdb_status.json", "w") as f:
        json.dump(_omdb_crawler_status, f)
    import subprocess
    subprocess.Popen(
        ["python3", "-u", "/app/omdb_rating_crawler_web.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return jsonify({"ok": True, "message": "OMDb评分爬虫已启动（IMDb+烂番茄+Metacritic）"})


@crawler_bp.route("/api/admin/omdb/status")
@admin_required
def api_omdb_status():
    """获取OMDb评分爬虫状态"""
    try:
        with open("/app/data/omdb_status.json", "r") as f:
            return jsonify(json.load(f))
    except:
        app.logger.exception("api_omdb_status failed")
        return jsonify(_omdb_crawler_status)
