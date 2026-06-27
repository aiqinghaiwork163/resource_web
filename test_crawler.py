#!/usr/bin/env python3
"""
多源影视爬虫 v1.0 - 快速测试版
只抓取少量数据用于测试
"""
import os
import sys
import json
import time
import socket
import ssl
import re
from datetime import datetime

import requests
import pymysql

# ── 配置 ──
SEARCH_API = os.environ.get("SEARCH_API", "https://ps.aiqinghaiwork.cn/api/search")
DB_HOST = os.environ.get("DB_HOST", "172.23.0.2")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD=os.environ.get("DB_PASSWORD", "9zPwwKV_YYK3IcivNFcXuA")
DB_NAME = os.environ.get("DB_NAME", "pan_resource")


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def search_api(kw):
    """搜索资源"""
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
    """导入数据库"""
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
    log("多源影视爬虫 - 快速测试版")
    log("=" * 60)
    
    # 测试数据源
    test_titles = [
        "Under the Dome",
        "Game of Thrones",
        "Breaking Bad",
        "Stranger Things",
        "The Last of Us",
    ]
    
    log(f"\n测试 {len(test_titles)} 个标题...")
    
    imp = 0
    for i, kw in enumerate(test_titles, 1):
        log(f"[{i}/{len(test_titles)}] 搜索: {kw}")
        items = search_api(kw)
        if items:
            n = import_db(items, kw)
            imp += n
            log(f"  找到 {len(items)} 个资源, 导入 {n} 条")
        else:
            log(f"  无结果")
        time.sleep(0.3)
    
    log(f"\n{'=' * 60}")
    log(f"测试完成! 导入 {imp} 条新资源")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
