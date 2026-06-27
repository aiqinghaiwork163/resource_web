#!/usr/bin/env python3
"""
TMDB 爬虫 v3.0 - Web集成版
支持：趋势/热门/影院/即将/评分/发现/相似推荐 递归爬取
独立进程运行，通过 JSON 文件与 Flask 应用通信
"""
import os
import sys
import json
import time
import socket
import ssl
from datetime import datetime
from collections import deque

import requests
import pymysql

STATUS_FILE = "/app/data/tmdb_status.json"
API_KEY = os.environ.get("TMDB_API_KEY")
if not API_KEY:
    raise RuntimeError("环境变量 TMDB_API_KEY 未设置")
SEARCH_API = os.environ.get("SEARCH_API", "https://ps.aiqinghaiwork.cn/api/search")
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "resource_db"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "panuser"),
    "password": os.environ.get("DB_PASSWORD"),
    "database": os.environ.get("DB_NAME", "pan_resource"),
    "charset": "utf8mb4",
}
if not DB_CONFIG["password"]:
    raise RuntimeError("环境变量 DB_PASSWORD 未设置")

# ── 爬取配置 ──
MAX_PAGES = 3
MAX_SIMILAR_DEPTH = 2
MAX_SIMILAR_PER_ITEM = 5
MAX_DISCOVER_PAGES = 2

# ── 基础榜单 ──
ENDPOINTS = {
    "趋势(日)": "trending/all/day",
    "趋势(周)": "trending/all/week",
    "电影-热门": "movie/popular",
    "电影-影院": "movie/now_playing",
    "电影-即将": "movie/upcoming",
    "电影-Top评分": "movie/top_rated",
    "剧集-热门": "tv/popular",
    "剧集-今日播出": "tv/airing_today",
    "剧集-正在播出": "tv/on_the_air",
    "剧集-Top评分": "tv/top_rated",
}

# ── Discover 发现接口 ──
DISCOVER_MOVIE = [
    {"label": "电影-发现-高分", "params": {"sort_by": "vote_average.desc", "vote_count.gte": 1000}},
    {"label": "电影-发现-2024热门", "params": {"sort_by": "popularity.desc", "primary_release_year": 2024}},
    {"label": "电影-发现-2025热门", "params": {"sort_by": "popularity.desc", "primary_release_year": 2025}},
    {"label": "电影-发现-动作", "params": {"sort_by": "popularity.desc", "with_genres": 28}},
    {"label": "电影-发现-科幻", "params": {"sort_by": "popularity.desc", "with_genres": 878}},
    {"label": "电影-发现-恐怖", "params": {"sort_by": "popularity.desc", "with_genres": 27}},
    {"label": "电影-发现-喜剧", "params": {"sort_by": "popularity.desc", "with_genres": 35}},
    {"label": "电影-发现-动画", "params": {"sort_by": "popularity.desc", "with_genres": 16}},
]

DISCOVER_TV = [
    {"label": "剧集-发现-高分", "params": {"sort_by": "vote_average.desc", "vote_count.gte": 500}},
    {"label": "剧集-发现-2024热门", "params": {"sort_by": "popularity.desc", "first_air_date_year": 2024}},
    {"label": "剧集-发现-2025热门", "params": {"sort_by": "popularity.desc", "first_air_date_year": 2025}},
    {"label": "剧集-发现-美剧", "params": {"sort_by": "popularity.desc", "with_origin_country": "US"}},
    {"label": "剧集-发现-韩剧", "params": {"sort_by": "popularity.desc", "with_origin_country": "KR"}},
    {"label": "剧集-发现-日剧", "params": {"sort_by": "popularity.desc", "with_origin_country": "JP"}},
    {"label": "剧集-发现-动漫", "params": {"sort_by": "popularity.desc", "with_genres": 16}},
]

status = {"running": True, "phase": "", "progress": 0, "total": 0, "imported": 0, "similar_count": 0, "log": []}


def save_status():
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, ensure_ascii=False)


def log(m):
    ts = datetime.now().strftime("%H:%M:%S")
    status["log"].append(f"[{ts}] {m}")
    # 只保留最近100条日志
    if len(status["log"]) > 100:
        status["log"] = status["log"][-100:]
    save_status()


def tmdb_fetch(ep, p=1, extra_params=None):
    """通过IP直连绕过GFW访问TMDB API"""
    try:
        ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
    except Exception:
        return None

    path = f"/3/{ep}?api_key={API_KEY}&language=zh-CN&page={p}"
    if extra_params:
        for k, v in extra_params.items():
            path += f"&{k}={v}"
    
    try:
        s = socket.create_connection((ip, 443), 10)
        ss = ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
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
            return None
        body = r[h + 4 :]
        if b"chunked" in r[:h]:
            lines = body.split(b"\r\n")
            decoded = b""
            for line in lines:
                if line and not all(c in b"0123456789abcdefABCDEF" for c in line[:8]):
                    decoded += line
            body = decoded
        return json.loads(body.decode())
    except Exception:
        return None


def extract_titles(data, label):
    """提取标题和ID"""
    results = []
    for item in data.get("results", []):
        title = item.get("title") or item.get("name", "")
        item_id = item.get("id")
        media_type = item.get("media_type", "")
        if title:
            results.append({
                "title": title.strip(),
                "id": item_id,
                "type": media_type,
                "source": label
            })
    return results


def fetch_similar(item_id, media_type="movie"):
    """获取相似推荐"""
    ep = f"{media_type}/{item_id}/similar"
    data = tmdb_fetch(ep)
    if not data:
        return []
    return extract_titles(data, "相似推荐")


def main():
    try:
        all_titles = []
        seen = set()
        
        # Phase 1: 基础榜单
        status["phase"] = "基础榜单"
        log("Phase 1: 基础榜单")
        for label, ep in ENDPOINTS.items():
            log(f"  [{label}] ...")
            for p in range(1, MAX_PAGES + 1):
                data = tmdb_fetch(ep, p)
                if not data:
                    break
                items = extract_titles(data, label)
                for t in items:
                    key = f"{t['id']}_{t['type']}"
                    if key not in seen:
                        seen.add(key)
                        all_titles.append(t)
                if p >= data.get("total_pages", 1):
                    break
                time.sleep(0.3)
            log(f"    共 {len(seen)} 个")
        
        # Phase 2: Discover 发现接口
        status["phase"] = "Discover发现"
        log("Phase 2: Discover 发现接口")
        all_discover = DISCOVER_MOVIE + DISCOVER_TV
        for disc in all_discover:
            label = disc["label"]
            params = disc["params"]
            ep = "discover/movie" if "电影" in label else "discover/tv"
            log(f"  [{label}] ...")
            for p in range(1, MAX_DISCOVER_PAGES + 1):
                data = tmdb_fetch(ep, p, params)
                if not data:
                    break
                items = extract_titles(data, label)
                for item in items:
                    if not item["type"]:
                        item["type"] = "movie" if "电影" in label else "tv"
                for t in items:
                    key = f"{t['id']}_{t['type']}"
                    if key not in seen:
                        seen.add(key)
                        all_titles.append(t)
                if p >= data.get("total_pages", 1):
                    break
                time.sleep(0.3)
        
        log(f"Phase 1+2 完成: {len(all_titles)} 个作品")
        
        # Phase 3: 递归相似推荐
        status["phase"] = "相似推荐"
        log(f"Phase 3: 相似推荐 (深度={MAX_SIMILAR_DEPTH})")
        
        queue = deque()
        for t in all_titles[:50]:
            if t["id"] and t["type"]:
                queue.append((t["id"], t["type"], 1))
        
        processed_similar = set()
        similar_count = 0
        
        while queue:
            item_id, media_type, depth = queue.popleft()
            if depth > MAX_SIMILAR_DEPTH:
                continue
            similar_key = f"{item_id}_{media_type}"
            if similar_key in processed_similar:
                continue
            processed_similar.add(similar_key)
            
            similar_items = fetch_similar(item_id, media_type)
            for t in similar_items[:MAX_SIMILAR_PER_ITEM]:
                key = f"{t['id']}_{t['type'] or media_type}"
                if key not in seen:
                    seen.add(key)
                    t["source"] = f"相似推荐(深度{depth})"
                    t["type"] = t["type"] or media_type
                    all_titles.append(t)
                    similar_count += 1
                    if depth < MAX_SIMILAR_DEPTH:
                        queue.append((t["id"], t["type"], depth + 1))
            
            status["similar_count"] = similar_count
            save_status()
            time.sleep(0.3)
        
        log(f"Phase 3 完成: 相似推荐新增 {similar_count} 个")
        log(f"总计: {len(all_titles)} 个作品")
        
        # Phase 4: 搜索并导入
        status["phase"] = "搜索导入"
        status["total"] = len(all_titles)
        log("Phase 4: 搜索并导入资源")
        
        db = pymysql.connect(**DB_CONFIG)
        cur = db.cursor()
        imported = 0
        
        for i, title_text in enumerate(all_titles, 1):
            status["progress"] = i
            kw = title_text["title"]
            
            if i % 50 == 0:
                log(f"  进度: {i}/{len(all_titles)}, 已导入: {imported}")
            
            try:
                resp = requests.post(SEARCH_API, json={"kw": kw, "limit": 30}, timeout=60)
                result = resp.json()
                if result.get("code") != 0:
                    continue
                merged = result.get("data", {}).get("merged_by_type", {})
                items = []
                for src, its in merged.items():
                    items.extend(its)
                
                added = 0
                nw = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for it in items:
                    url = it.get("url", "")
                    if not url:
                        continue
                    cur.execute("SELECT 1 FROM resources WHERE url=%s", (url,))
                    if cur.fetchone():
                        continue
                    title = it.get("note") or it.get("title") or kw
                    src = (it.get("source") or "other").lower()
                    cur.execute(
                        "INSERT INTO resources(url,title,keyword,password,source,datetime,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (url, title, kw, it.get("password", ""), src, nw, nw),
                    )
                    added += 1
                db.commit()
                imported += added
                status["imported"] = imported
            except Exception as e:
                log(f"  Error: {str(e)[:60]}")
            
            save_status()
            time.sleep(0.2)
        
        cur.close()
        db.close()
        log(f"\n🎉 完成! {len(all_titles)} 作品, {imported} 新资源")
        
    except Exception as e:
        log(f"\nError: {e}")
    finally:
        status["running"] = False
        status["phase"] = "完成"
        save_status()


if __name__ == "__main__":
    main()
