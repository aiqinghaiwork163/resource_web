#!/usr/bin/env python3
"""
TMDB 爬虫 v3.0 - 终极版
支持：趋势/热门/影院/即将/评分/发现/相似推荐 递归爬取
绕过GFW直连TMDB API，资源量指数级增长
"""
import os
import sys
import json
import time
from datetime import datetime
from collections import deque

import socket
import ssl
import threading
import struct
import requests
import pymysql

# ── 配置（优先环境变量，无默认值强制设置） ──
API_KEY = os.environ.get("TMDB_API_KEY")
if not API_KEY:
    raise RuntimeError("环境变量 TMDB_API_KEY 未设置")
SEARCH_API = os.environ.get("SEARCH_API", "https://ps.aiqinghaiwork.cn/api/search")
DB_HOST = os.environ.get("DB_HOST", "172.23.0.2")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
if not DB_PASSWORD:
    raise RuntimeError("环境变量 DB_PASSWORD 未设置")
DB_NAME = os.environ.get("DB_NAME", "pan_resource")

# ── 爬取配置 ──
MAX_PAGES = 3           # 每个列表接口抓几页（1-5）
MAX_SIMILAR_DEPTH = 1   # 递归相似推荐深度（1-3）
MAX_SIMILAR_PER_ITEM = 5  # 每部作品抓几个相似推荐
MAX_DISCOVER_PAGES = 2  # discover接口抓几页

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

# ── Discover 发现接口（按类型/年份/评分筛选） ──
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


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# DNS cache
_TMDB_IP = None
def _resolve_ip():
    global _TMDB_IP
    if _TMDB_IP:
        return _TMDB_IP
    try:
        import socket as _sock
        _TMDB_IP = _sock.getaddrinfo("www.themoviedb.org", 443, _sock.AF_INET)[0][4][0]
    except Exception:
        pass
    return _TMDB_IP

_resolve_ip()

def tmdb_fetch(ep, p=1, extra_params=None):
    """通过IP直连绕过GFW访问TMDB API"""
    import socket, ssl
    ip = _TMDB_IP or _resolve_ip()
    if not ip:
        return None
    path = f"/3/{ep}?api_key={API_KEY}&language=zh-CN&page={p}"
    if extra_params:
        for k, v in extra_params.items():
            path += f"&{k}={v}"
    s = None
    try:
        s = socket.create_connection((ip, 443), 10)
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(s, server_hostname="api.themoviedb.org")
        ss.settimeout(15)
        ss.sendall(
            f"GET {path} HTTP/1.1\r\nHost: api.themoviedb.org\r\n"
            f"Accept: application/json\r\nConnection: close\r\n\r\n".encode()
        )
        r = b""
        while True:
            try:
                c = ss.recv(65536)
                if not c:
                    break
                r += c
            except socket.timeout:
                log(f"  ⚠ {ep}: recv timeout")
                break
        try:
            ss.close()
        except:
            pass
        h = r.find(b"\r\n\r\n")
        if h < 0:
            return None
        b = r[h + 4:]
        if b"chunked" in r[:h]:
            r2 = b
            b = b""
            while r2:
                e = r2.find(b"\r\n")
                if e < 0:
                    break
                try:
                    sz = int(r2[:e], 16)
                except ValueError:
                    break
                r2 = r2[e + 2:]
                if sz == 0:
                    break
                b += r2[:sz]
                r2 = r2[sz + 2:]
        if not b:
            return None
        return json.loads(b.decode())
    except Exception as e:
        log(f"  ✗ {ep}: {e}")
        return None
    finally:
        if s:
            try:
                s.close()
            except:
                pass


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


def fetch_list_pages(ep, max_pages=MAX_PAGES, extra_params=None):
    """抓取多页列表数据"""
    all_items = []
    seen_ids = set()
    for p in range(1, max_pages + 1):
        data = tmdb_fetch(ep, p, extra_params)
        if not data:
            break
        items = extract_titles(data, "")
        for item in items:
            if item["id"] and item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                all_items.append(item)
        if p >= data.get("total_pages", 1):
            break
        time.sleep(0.3)
    return all_items


def fetch_similar(item_id, media_type="movie"):
    """获取相似推荐"""
    ep = f"{media_type}/{item_id}/similar"
    data = tmdb_fetch(ep)
    if not data:
        return []
    return extract_titles(data, "相似推荐")


def search_api(kw):
    try:
        resp = requests.post(SEARCH_API, json={"kw": kw, "limit": 30}, timeout=15)
        result = resp.json()
        merged = result.get("data", {}).get("merged_by_type", {})
        all_items = []
        for src, items in merged.items():
            all_items.extend(items)
        return all_items
    except Exception as e:
        log(f"  ✗ 搜[{kw}]: {e}")
        return []


def import_db(items, kw):
    if not items:
        return 0
    try:
        c = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset="utf8mb4",
            connect_timeout=5,
        )
        cur = c.cursor()
        nw = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        imp = 0
        for it in items:
            url = it.get("url") or ""
            if not url:
                continue
            title = it.get("note") or it.get("title") or kw
            pw = it.get("password") or ""
            src = (it.get("source") or it.get("type") or "other").lower()
            cur.execute("SELECT 1 FROM resources WHERE url=%s", (url,))
            if cur.fetchone():
                continue
            cur.execute(
                "INSERT INTO resources(url,title,keyword,password,source,datetime,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (url, title, kw, pw, src, nw, nw),
            )
            imp += 1
        c.commit()
        cur.close()
        c.close()
        return imp
    except Exception as e:
        log(f"  ✗ DB导入: {e}")
        return 0


def main():
    log("=" * 60)
    log("TMDB 爬虫 v3.0 - 终极版 启动")
    log(f"  基础榜单: {len(ENDPOINTS)} 个")
    log(f"  电影发现: {len(DISCOVER_MOVIE)} 个")
    log(f"  剧集发现: {len(DISCOVER_TV)} 个")
    log(f"  递归深度: {MAX_SIMILAR_DEPTH}, 每部抓相似: {MAX_SIMILAR_PER_ITEM}")
    log("=" * 60)
    
    all_titles = []  # (title, id, media_type, source)
    seen = set()
    
    # Phase 1: 基础榜单
    log("\n📡 Phase 1: 基础榜单")
    for label, ep in ENDPOINTS.items():
        log(f"\n  [{label}] ...")
        for p in range(1, MAX_PAGES + 1):
            data = tmdb_fetch(ep, p)
            if not data:
                break
            items = extract_titles(data, label)
            b4 = len(seen)
            for t in items:
                key = f"{t['id']}_{t['type']}"
                if key not in seen:
                    seen.add(key)
                    all_titles.append(t)
            log(f"    页{p}: +{len(seen) - b4} (共{len(seen)})")
            if p >= data.get("total_pages", 1):
                break
            time.sleep(0.3)
    
    # Phase 2: Discover 发现接口
    log(f"\n📡 Phase 2: Discover 发现接口")
    
    all_discover = DISCOVER_MOVIE + DISCOVER_TV
    for disc in all_discover:
        label = disc["label"]
        params = disc["params"]
        log(f"\n  [{label}] ...")
        ep = "discover/movie" if "电影" in label else "discover/tv"
        
        for p in range(1, MAX_DISCOVER_PAGES + 1):
            data = tmdb_fetch(ep, p, params)
            if not data:
                break
            items = extract_titles(data, label)
            # 设置正确的media_type
            for item in items:
                if not item["type"]:
                    item["type"] = "movie" if "电影" in label else "tv"
            b4 = len(seen)
            for t in items:
                key = f"{t['id']}_{t['type']}"
                if key not in seen:
                    seen.add(key)
                    all_titles.append(t)
            log(f"    页{p}: +{len(seen) - b4} (共{len(seen)})")
            if p >= data.get("total_pages", 1):
                break
            time.sleep(0.3)
    
    log(f"\n📊 Phase 1+2 完成: {len(all_titles)} 个作品")
    
    # Phase 3: 递归相似推荐
    log(f"\n📡 Phase 3: 相似推荐 (深度={MAX_SIMILAR_DEPTH})")
    
    # 从热门作品开始递归
    queue = deque()
    for t in all_titles[:50]:  # 取前50个热门作品开始
        if t["id"] and t["type"]:
            queue.append((t["id"], t["type"], 1))  # (id, type, depth)
    
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
        
        log(f"  [深度{depth}] {media_type}/{item_id} 的相似推荐...")
        similar_items = fetch_similar(item_id, media_type)
        
        added = 0
        for t in similar_items[:MAX_SIMILAR_PER_ITEM]:
            key = f"{t['id']}_{t['type'] or media_type}"
            if key not in seen:
                seen.add(key)
                t["source"] = f"相似推荐(深度{depth})"
                t["type"] = t["type"] or media_type
                all_titles.append(t)
                added += 1
                # 加入队列继续递归
                if depth < MAX_SIMILAR_DEPTH:
                    queue.append((t["id"], t["type"], depth + 1))
        
        similar_count += added
        if added:
            log(f"    +{added} (相似总计: {similar_count})")
        time.sleep(0.3)
    
    log(f"\n📊 Phase 3 完成: 相似推荐新增 {similar_count} 个")
    log(f"📊 总计: {len(all_titles)} 个作品")
    
    # 保存标题到JSON文件供后续导入
    with open("/tmp/tmdb_titles.json", "w") as f:
        json.dump(all_titles, f, ensure_ascii=False)
    log(f"💾 已保存 {len(all_titles)} 个标题到 /tmp/tmdb_titles.json")
    
    # Phase 4: 搜索并导入
    log(f"\n📡 Phase 4: 搜索并导入资源")
    imp = 0
    for i, t in enumerate(all_titles, 1):
        kw = t["title"]
        if i % 50 == 0 or i == len(all_titles):
            log(f"\n  进度: {i}/{len(all_titles)}, 已导入: {imp}")
        its = search_api(kw)
        if its:
            n = import_db(its, kw)
            imp += n
        time.sleep(0.2)
    
    log(f"\n{'=' * 60}")
    log(f"🎉 完成!")
    log(f"  作品总数: {len(all_titles)}")
    log(f"  新增资源: {imp}")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
