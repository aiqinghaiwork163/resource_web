#!/usr/bin/env python3
"""
TMDB 爬虫 - Web集成版
独立进程运行，通过 JSON 文件与 Flask 应用通信
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

STATUS_FILE = "/app/data/tmdb_status.json"
API_KEY = "91f75e1241354f2503a397fc7a276408"
SEARCH_API = "https://ps.aiqinghaiwork.cn/api/search"
DB_CONFIG = {
    "host": "resource_db",
    "port": 3306,
    "user": "panuser",
    "password": "panuser_2026",
    "database": "pan_resource",
    "charset": "utf8mb4",
}

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

status = {"running": True, "progress": 0, "total": 0, "imported": 0, "log": []}


def save_status():
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, ensure_ascii=False)


def log(m):
    ts = datetime.now().strftime("%H:%M:%S")
    status["log"].append(f"[{ts}] {m}")
    save_status()


def tmdb_get(ep, p=1):
    try:
        ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
        path = f"/3/{ep}?api_key={API_KEY}&language=zh-CN&page={p}"
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


def main():
    try:
        # Phase 1: Fetch TMDB titles
        log("Phase 1: Getting TMDB trending titles...")
        titles = []
        seen = set()
        for label, ep in ENDPOINTS.items():
            log(f"  [{label}] ...")
            data = tmdb_get(ep)
            if not data:
                log(f"  Failed to fetch")
                continue
            count = 0
            for item in data.get("results", []):
                t = (item.get("title") or item.get("name", "")).strip()
                k = t.lower()
                if t and k not in seen:
                    seen.add(k)
                    titles.append(t)
                    count += 1
            log(f"  +{count} (total {len(seen)})")
            time.sleep(0.3)

        status["total"] = len(titles)
        save_status()
        log(f"Total {len(titles)} titles\n")

        # Phase 2: Search and import
        log("Phase 2: Searching and importing...")
        db = pymysql.connect(**DB_CONFIG)
        cur = db.cursor()
        imported = 0

        for i, title in enumerate(titles, 1):
            status["progress"] = i
            log(f"[{i}/{len(titles)}] {title}")
            try:
                resp = requests.post(SEARCH_API, json={"kw": title, "limit": 30}, timeout=60)
                result = resp.json()
                if result.get("code") != 0:
                    continue
                merged = result.get("data", {}).get("merged_by_type", {})
                items = []
                for src, its in merged.items():
                    items.extend(its)
                if not items:
                    log("  No results")
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
                        (url, title_text, title, it.get("password", ""), src, nw, nw),
                    )
                    added += 1
                db.commit()
                imported += added
                status["imported"] = imported
                log(f"  +{added}" if added else "  Already exists")
            except Exception as e:
                log(f"  Error: {str(e)[:60]}")
            save_status()
            time.sleep(0.3)

        cur.close()
        db.close()
        log(f"\nDone! {len(titles)} titles, {imported} new resources")
    except Exception as e:
        log(f"\nError: {e}")
    finally:
        status["running"] = False
        save_status()


if __name__ == "__main__":
    main()
