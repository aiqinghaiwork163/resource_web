#!/usr/bin/env python3
"""
TMDB 爬虫 v2.0 - 使用 requests 库，API密钥走环境变量
绕过GFW直连TMDB API，获取趋势/热门/流媒体/电视/影院
资源名称，通过外部搜索API查询，自动导入数据库。
"""
import os
import sys
import json
import time
import socket
import ssl
from datetime import datetime

import requests
import pymysql

# ── 配置（优先环境变量） ──
API_KEY = os.environ.get("TMDB_API_KEY", "91f75e1241354f2503a397fc7a276408")
SEARCH_API = os.environ.get("SEARCH_API", "https://ps.aiqinghaiwork.cn/api/search")
DB_HOST = os.environ.get("DB_HOST", "172.23.0.2")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "9zPwwKV_YYK3IcivNFcXuA")
DB_NAME = os.environ.get("DB_NAME", "pan_resource")

ENDPOINTS = {
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def tmdb_fetch(ep, p=1):
    """通过IP直连绕过GFW访问TMDB API"""
    try:
        ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
    except Exception:
        return None

    path = f"/3/{ep}?api_key={API_KEY}&language=zh-CN&page={p}"
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
        b = r[h + 4 :]
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
                r2 = r2[e + 2 :]
                if sz == 0:
                    break
                b += r2[:sz]
                r2 = r2[sz + 2 :]
        return json.loads(b.decode())
    except Exception as e:
        log(f"  ✗ {ep}: {e}")
        return None


def extract(da, lb):
    r = []
    for i in da.get("results", []):
        t = i.get("title") or i.get("name", "")
        if t:
            r.append({"title": t.strip(), "source": lb})
    return r


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
    log("=" * 50)
    log("TMDB 爬虫启动")
    titles = []
    seen = set()
    for lb, ep in ENDPOINTS.items():
        log(f"\n📡 [{lb}] ...")
        da = tmdb_fetch(ep)
        if not da:
            continue
        items = extract(da, lb)
        b4 = len(seen)
        for t in items:
            k = t["title"].lower().strip()
            if k not in seen:
                seen.add(k)
                titles.append(t)
        log(f"  +{len(seen) - b4} (共{len(seen)})")
        time.sleep(0.5)

    log(f"\n📊 共{len(titles)}个标题，开始搜索\n")
    imp = 0
    for i, t in enumerate(titles, 1):
        kw = t["title"]
        log(f"[{i}/{len(titles)}] 🔍 {kw}")
        its = search_api(kw)
        if its:
            n = import_db(its, kw)
            imp += n
            log(f"  ✅ +{n}" if n else "  ℹ️ 已存在")
        else:
            log("  ℹ️ 无结果")
        time.sleep(0.3)

    log(f"\n📊 汇总: {len(titles)} 标题, 新增 {imp} 条")
    log("=" * 50)


if __name__ == "__main__":
    main()
