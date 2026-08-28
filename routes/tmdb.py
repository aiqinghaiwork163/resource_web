"""TMDB and image proxy routes."""
import json
import socket
import ssl as _ssl
import time
import urllib.parse
from collections import OrderedDict

import requests as _req
from flask import Blueprint, jsonify, request

from models.database import get_db
from services.tmdb_service import get_tmdb_api_key
from utils.memory_cache import memory_cache

tmdb_bp = Blueprint("tmdb", __name__)

_img_cache: OrderedDict = OrderedDict()
_IMG_CACHE_MAX = 1000


def img_cache_evict():
    while len(_img_cache) > _IMG_CACHE_MAX:
        _img_cache.popitem(last=False)
@tmdb_bp.route("/api/img_proxy")
def img_proxy():
    """代理 TMDB 图片，本地内存缓存 24 小时"""
    url = request.args.get("url", "")
    if not url or "tmdb.org" not in url:
        return "", 400
    now = time.time()
    if url in _img_cache:
        data, ct, expire = _img_cache[url]
        if now < expire:
            return data, 200, {"Content-Type": ct, "Cache-Control": "public, max-age=86400"}
    try:
        import requests as _req
        r = _req.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            ct = r.headers.get("Content-Type", "image/jpeg")
            _img_cache[url] = (r.content, ct, now + 86400)
            img_cache_evict()  # 超限时淘汰旧条目
            return r.content, 200, {"Content-Type": ct, "Cache-Control": "public, max-age=86400"}
    except Exception:
        pass
    return "", 404

@tmdb_bp.route("/api/tmdb/trending")
def api_tmdb_trending():
    """TMDB 热门排行（绕GFW直连），带缓存+超时+故障降级"""
    import socket, ssl as _ssl, json as _json, urllib.parse
    # 兼容前端 type 参数：week/day/movie/tv
    req_type = request.args.get("type", "")
    if req_type in ("movie", "tv"):
        media = req_type
        time_window = "week"
    elif req_type in ("day",):
        media = "all"
        time_window = "day"
    else:
        media = request.args.get("media", "all")
        time_window = request.args.get("time", "week")
    page = min(int(request.args.get("page", 1)), 10)
    API_KEY = get_tmdb_api_key()
    TMDB_TTL = 1800  # 30 minutes cache

    # 生成缓存 key
    cache_key = f"tmdb_trending:{media}:{time_window}:{page}"
    now = time.time()

    # 检查缓存
    if cache_key in memory_cache:
        cached_data, expire = memory_cache[cache_key]
        if now < expire:
            return jsonify(cached_data)

    ep = f"trending/{media}/{time_window}"
    path = f"/3/{ep}?api_key={API_KEY}&language=zh-CN&page={page}"
    try:
        ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
        s = socket.create_connection((str(ip), 443), timeout=5)
        s.settimeout(5)  # 读取也限5秒
        ss = _ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
        ss.settimeout(5)
        ss.sendall(
            f"GET {path} HTTP/1.1\r\nHost: api.themoviedb.org\r\n"
            f"Accept: application/json\r\nConnection: close\r\n\r\n".encode()
        )
        r = b""
        while True:
            c = ss.recv(65536)
            if not c:
                break
            r += c
        ss.close()
        h = r.find(b"\r\n\r\n")
        if h < 0:
            # 无响应头，降级返回缓存或空
            if cache_key in memory_cache:
                return jsonify(memory_cache[cache_key][0])
            return jsonify({"items": [], "error": "no response"})
        body = r[h + 4:]
        if b"chunked" in r[:h]:
            r2 = body; body = b""
            while r2:
                e = r2.find(b"\r\n")
                if e < 0:
                    break
                try:
                    sz = int(r2[:e], 16)
                except ValueError:
                    break
                if sz == 0:
                    break
                body += r2[e + 2:e + 2 + sz]
                r2 = r2[e + 2 + sz + 2:]
        data = _json.loads(body)
        items = []
        for it in data.get("results", [])[:20]:
            items.append({
                "title": it.get("title") or it.get("name") or "",
                "overview": (it.get("overview") or "")[:200],
                "poster": ("/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w500" + it["poster_path"])) if it.get("poster_path") else "",
                "backdrop": ("/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w780" + it["backdrop_path"])) if it.get("backdrop_path") else "",
                "rating": round(it.get("vote_average", 0), 1),
                "media_type": it.get("media_type", ""),
                "year": (it.get("release_date") or it.get("first_air_date") or "")[:4],
                "tmdb_id": it.get("id"),
            })
        result_data = {"items": items, "total": data.get("total_results", 0)}
        # 写入缓存（存数据字典，不存response对象，防止gzip中间件重复压缩）
        memory_cache[cache_key] = (result_data, now + TMDB_TTL)
        return jsonify(result_data)
    except Exception as e:
        # 外部API故障，返回缓存的旧数据
        if cache_key in memory_cache:
            return jsonify(memory_cache[cache_key][0])
        return jsonify({"items": [], "error": str(e)})
