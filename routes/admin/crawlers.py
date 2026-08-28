"""Admin crawler controls."""
import json
import subprocess
import time
from datetime import datetime

from flask import jsonify

from models.database import get_db
from services.tmdb_service import get_tmdb_api_key
from utils.auth import admin_required

from routes.admin.misc import admin_bp

_tmdb_crawler_status = {"running": False, "progress": 0, "total": 0, "imported": 0, "log": []}
_multi_crawler_status = {"running": False, "phase": "", "progress": 0, "total": 0, "imported": 0, "log": []}
_omdb_crawler_status = {"running": False, "progress": 0, "total": 0, "success": 0, "failed": 0, "log": []}
@admin_bp.route("/api/admin/tmdb/crawl", methods=["POST"])
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

@admin_bp.route("/api/admin/tmdb/status")
@admin_required
def api_tmdb_status():
    """获取 TMDB 爬虫状态（从 JSON 文件读取）"""
    try:
        with open("/app/data/tmdb_status.json", "r") as f:
            return jsonify(json.load(f))
    except:
        return jsonify(_tmdb_crawler_status)

def _tmdb_crawler_worker():
    """TMDB 爬虫后台工作线程"""
    import socket as _sock
    import ssl as _ssl
    import requests as _req
    import json as _json

    TMDB_KEY = API_KEY = get_tmdb_api_key()
    SEARCH_URL = "https://ps.aiqinghaiwork.cn/api/search"
    endpoints = {
        "趋势(日)": "trending/all/day",
        "趋势(周)": "trending/all/week",
        "电影-热门": "movie/popular",
        "电影-影院": "movie/now_playing",
        "电影-即将": "movie/upcoming",
        "剧集-热门": "tv/popular",
        "剧集-今日播出": "tv/airing_today",
        "剧集-正在播出": "tv/on_the_air",
    }

    def log(m):
        ts = datetime.now().strftime("%H:%M:%S")
        _tmdb_crawler_status["log"].append(f"[{ts}] {m}")

    def tmdb_get(ep, p=1):
        try:
            ip = _sock.getaddrinfo("www.themoviedb.org", 443, _sock.AF_INET)[0][4][0]
            path = f"/3/{ep}?api_key={TMDB_KEY}&language=zh-CN&page={p}"
            s = _sock.create_connection((ip, 443), 10)
            ss = _ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
            ss.sendall(f"GET {path} HTTP/1.1\r\nHost: api.themoviedb.org\r\nAccept: application/json\r\nConnection: close\r\n\r\n".encode())
            r = b""
            while True:
                c = ss.recv(65536)
                if not c: break
                r += c
            ss.close()
            h = r.find(b"\r\n\r\n")
            if h < 0: return None
            body = r[h+4:]
            if b"chunked" in r[:h]:
                lines = body.split(b"\r\n")
                decoded = b""
                for line in lines:
                    if line and not all(c in b"0123456789abcdefABCDEF" for c in line[:8]):
                        decoded += line
                body = decoded
            return _json.loads(body.decode())
        except:
            return None

    try:
        # Phase 1: Fetch TMDB titles
        log("📡 Phase 1: 获取 TMDB 热门标题...")
        titles = []
        seen = set()
        for label, ep in endpoints.items():
            log(f"  [{label}] ...")
            data = tmdb_get(ep)
            if not data:
                log(f"  ✗ 获取失败")
                continue
            count = 0
            for item in data.get("results", []):
                t = (item.get("title") or item.get("name", "")).strip()
                k = t.lower()
                if t and k not in seen:
                    seen.add(k)
                    titles.append(t)
                    count += 1
            log(f"  +{count} (共{len(seen)})")
            time.sleep(0.3)

        _tmdb_crawler_status["total"] = len(titles)
        log(f"📊 共{len(titles)}个标题\n")

        # Phase 2: Search and import
        log("🔍 Phase 2: 搜索网盘资源并导入...")
        db = get_db()
        cur = db.cursor()
        imported = 0

        for i, title in enumerate(titles, 1):
            _tmdb_crawler_status["progress"] = i
            log(f"[{i}/{len(titles)}] 🔍 {title}")
            try:
                resp = _req.post(SEARCH_URL, json={"kw": title, "limit": 30}, timeout=60)
                result = resp.json()
                if result.get("code") != 0:
                    continue
                merged = result.get("data", {}).get("merged_by_type", {})
                items = []
                for src, its in merged.items():
                    items.extend(its)
                if not items:
                    log("  ℹ️ 无结果")
                    continue

                added = 0
                nw = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for it in items:
                    url = it.get("url", "")
                    if not url:
                        continue
                    cur.execute("SELECT 1 FROM resources WHERE url=%s", (url,))
                    if cur.fetchone():
                        continue
                    title_text = it.get("note") or it.get("title") or title
                    src = (it.get("source") or "other").lower()
                    cur.execute(
                        "INSERT INTO resources(url,title,keyword,password,source,datetime,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (url, title_text, title, it.get("password", ""), src, nw, nw)
                    )
                    added += 1
                db.commit()
                imported += added
                _tmdb_crawler_status["imported"] = imported
                log(f"  ✅ +{added}" if added else "  ℹ️ 已存在")
            except Exception as e:
                log(f"  ✗ 错误: {str(e)[:60]}")
            time.sleep(0.3)

        cur.close()
        db.close()
        log(f"\n🎉 完成！共{len(titles)}个标题，新增{imported}条资源")
    except Exception as e:
        log(f"\n❌ 爬虫异常: {e}")
    finally:
        _tmdb_crawler_status["running"] = False

@admin_bp.route("/api/admin/multi-crawl", methods=["POST"])
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

@admin_bp.route("/api/admin/multi-crawl/status")
@admin_required
def api_multi_crawl_status():
    """获取多源爬虫状态"""
    try:
        with open("/app/data/multi_source_status.json", "r") as f:
            return jsonify(json.load(f))
    except:
        return jsonify(_multi_crawler_status)

@admin_bp.route("/api/admin/omdb/crawl", methods=["POST"])
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

@admin_bp.route("/api/admin/omdb/status")
@admin_required
def api_omdb_status():
    """获取OMDb评分爬虫状态"""
    try:
        with open("/app/data/omdb_status.json", "r") as f:
            return jsonify(json.load(f))
    except:
        return jsonify(_omdb_crawler_status)
