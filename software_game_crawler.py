#!/usr/bin/env python3
"""
通用资源爬虫 v1.0
支持：软件/工具/游戏/影视 等多种类型
数据源：GitHub Trending、Steam、软件下载站、游戏平台
"""
import os
import sys
import json
import time
import requests
from datetime import datetime

import pymysql

# ── 配置 ──
SEARCH_API = os.environ.get("SEARCH_API", "https://ps.aiqinghaiwork.cn/api/search")
DB_HOST = os.environ.get("DB_HOST", "172.23.0.2")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "9zPwwKV_YYK3IcivNFcXuA")
DB_NAME = os.environ.get("DB_NAME", "pan_resource")


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ══════════════════════════════════════════════════════════════
#  GitHub Trending (开源软件/工具)
# ══════════════════════════════════════════════════════════════
class GitHubCrawler:
    """GitHub Trending - 开源软件/工具"""
    
    def fetch_trending(self, language="", since="daily", limit=30):
        """获取GitHub Trending项目"""
        items = []
        try:
            url = "https://api.github.com/search/repositories"
            params = {
                "q": f"stars:>1000 {'language:' + language if language else ''}",
                "sort": "stars",
                "order": "desc",
                "per_page": limit
            }
            headers = {"Accept": "application/vnd.github.v3+json"}
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            data = resp.json()
            
            for repo in data.get("items", []):
                items.append({
                    "title": repo["name"],
                    "description": repo.get("description", ""),
                    "category": "软件",
                    "subcategory": "开源工具",
                    "source": "GitHub-Trending",
                    "url": repo["html_url"],
                    "stars": repo["stargazers_count"]
                })
            log(f"  GitHub Trending: {len(items)} 个")
        except Exception as e:
            log(f"  GitHub Trending 失败: {e}")
        return items
    
    def fetch_topics(self, topic, limit=20):
        """获取特定主题的项目"""
        items = []
        try:
            url = "https://api.github.com/search/repositories"
            params = {
                "q": f"topic:{topic} stars:>500",
                "sort": "stars",
                "order": "desc",
                "per_page": limit
            }
            headers = {"Accept": "application/vnd.github.v3+json"}
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            data = resp.json()
            
            for repo in data.get("items", []):
                items.append({
                    "title": repo["name"],
                    "description": repo.get("description", ""),
                    "category": "软件",
                    "subcategory": topic,
                    "source": f"GitHub-{topic}",
                    "url": repo["html_url"],
                    "stars": repo["stargazers_count"]
                })
            log(f"  GitHub {topic}: {len(items)} 个")
        except Exception as e:
            log(f"  GitHub {topic} 失败: {e}")
        return items


# ══════════════════════════════════════════════════════════════
#  Steam (游戏)
# ══════════════════════════════════════════════════════════════
class SteamCrawler:
    """Steam - 热门游戏"""
    
    def fetch_popular(self, limit=50):
        """获取Steam热门游戏"""
        items = []
        try:
            # Steam API获取热门游戏
            url = "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"
            resp = requests.get(url, timeout=15)
            data = resp.json()
            
            games = data.get("response", {}).get("ranks", [])[:limit]
            for game in games:
                items.append({
                    "title": game.get("name", ""),
                    "category": "游戏",
                    "subcategory": "PC游戏",
                    "source": "Steam-热门",
                    "appid": game.get("appid"),
                    "players": game.get("players", 0)
                })
            log(f"  Steam 热门: {len(items)} 个")
        except Exception as e:
            log(f"  Steam 热门失败: {e}")
        return items
    
    def fetch_specials(self, limit=30):
        """获取Steam特惠游戏"""
        items = []
        try:
            url = "https://store.steampowered.com/api/featuredcategories"
            resp = requests.get(url, timeout=15)
            data = resp.json()
            
            specials = data.get("specials", {}).get("items", [])[:limit]
            for game in specials:
                items.append({
                    "title": game.get("name", ""),
                    "category": "游戏",
                    "subcategory": "Steam特惠",
                    "source": "Steam-特惠",
                    "appid": game.get("id"),
                    "price": game.get("final_price", 0) / 100
                })
            log(f"  Steam 特惠: {len(items)} 个")
        except Exception as e:
            log(f"  Steam 特惠失败: {e}")
        return items


# ══════════════════════════════════════════════════════════════
#  常用软件/工具列表
# ══════════════════════════════════════════════════════════════
SOFTWARE_LIST = [
    # 开发工具
    {"name": "Visual Studio Code", "category": "软件", "subcategory": "开发工具", "keyword": "VSCode"},
    {"name": "JetBrains IDE", "category": "软件", "subcategory": "开发工具", "keyword": "JetBrains"},
    {"name": "Sublime Text", "category": "软件", "subcategory": "开发工具", "keyword": "Sublime Text"},
    {"name": "Navicat", "category": "软件", "subcategory": "数据库工具", "keyword": "Navicat"},
    {"name": "DataGrip", "category": "软件", "subcategory": "数据库工具", "keyword": "DataGrip"},
    {"name": "DBeaver", "category": "软件", "subcategory": "数据库工具", "keyword": "DBeaver"},
    {"name": "Postman", "category": "软件", "subcategory": "API工具", "keyword": "Postman"},
    {"name": "Apifox", "category": "软件", "subcategory": "API工具", "keyword": "Apifox"},
    
    # 设计工具
    {"name": "Adobe Photoshop", "category": "软件", "subcategory": "设计工具", "keyword": "Photoshop"},
    {"name": "Adobe Illustrator", "category": "软件", "subcategory": "设计工具", "keyword": "Illustrator"},
    {"name": "Adobe Premiere", "category": "软件", "subcategory": "视频编辑", "keyword": "Premiere"},
    {"name": "Adobe After Effects", "category": "软件", "subcategory": "视频编辑", "keyword": "After Effects"},
    {"name": "Figma", "category": "软件", "subcategory": "设计工具", "keyword": "Figma"},
    {"name": "Sketch", "category": "软件", "subcategory": "设计工具", "keyword": "Sketch"},
    {"name": "Canva", "category": "软件", "subcategory": "设计工具", "keyword": "Canva"},
    
    # 办公软件
    {"name": "Microsoft Office", "category": "软件", "subcategory": "办公软件", "keyword": "Office"},
    {"name": "WPS Office", "category": "软件", "subcategory": "办公软件", "keyword": "WPS"},
    {"name": "Notion", "category": "软件", "subcategory": "笔记工具", "keyword": "Notion"},
    {"name": "Obsidian", "category": "软件", "subcategory": "笔记工具", "keyword": "Obsidian"},
    {"name": "Typora", "category": "软件", "subcategory": "笔记工具", "keyword": "Typora"},
    
    # 系统工具
    {"name": "Windows 11", "category": "软件", "subcategory": "操作系统", "keyword": "Windows 11"},
    {"name": "macOS", "category": "软件", "subcategory": "操作系统", "keyword": "macOS"},
    {"name": "VMware", "category": "软件", "subcategory": "虚拟机", "keyword": "VMware"},
    {"name": "Parallels Desktop", "category": "软件", "subcategory": "虚拟机", "keyword": "Parallels"},
    
    # 网络工具
    {"name": "Chrome", "category": "软件", "subcategory": "浏览器", "keyword": "Chrome"},
    {"name": "Firefox", "category": "软件", "subcategory": "浏览器", "keyword": "Firefox"},
    {"name": "Clash", "category": "软件", "subcategory": "网络工具", "keyword": "Clash"},
    {"name": "V2Ray", "category": "软件", "subcategory": "网络工具", "keyword": "V2Ray"},
    
    # 下载工具
    {"name": "IDM", "category": "软件", "subcategory": "下载工具", "keyword": "IDM"},
    {"name": "迅雷", "category": "软件", "subcategory": "下载工具", "keyword": "迅雷"},
    {"name": "qBittorrent", "category": "软件", "subcategory": "下载工具", "keyword": "qBittorrent"},
    
    # 压缩工具
    {"name": "WinRAR", "category": "软件", "subcategory": "压缩工具", "keyword": "WinRAR"},
    {"name": "7-Zip", "category": "软件", "subcategory": "压缩工具", "keyword": "7-Zip"},
    
    # 远程工具
    {"name": "TeamViewer", "category": "软件", "subcategory": "远程工具", "keyword": "TeamViewer"},
    {"name": "向日葵", "category": "软件", "subcategory": "远程工具", "keyword": "向日葵"},
    {"name": "ToDesk", "category": "软件", "subcategory": "远程工具", "keyword": "ToDesk"},
]

# 热门游戏列表
GAME_LIST = [
    {"name": "黑神话悟空", "category": "游戏", "subcategory": "PC游戏", "keyword": "黑神话悟空"},
    {"name": "艾尔登法环", "category": "游戏", "subcategory": "PC游戏", "keyword": "艾尔登法环"},
    {"name": "GTA5", "category": "游戏", "subcategory": "PC游戏", "keyword": "GTA5"},
    {"name": "赛博朋克2077", "category": "游戏", "subcategory": "PC游戏", "keyword": "赛博朋克2077"},
    {"name": "荒野大镖客2", "category": "游戏", "subcategory": "PC游戏", "keyword": "荒野大镖客2"},
    {"name": "只狼", "category": "游戏", "subcategory": "PC游戏", "keyword": "只狼"},
    {"name": "怪物猎人", "category": "游戏", "subcategory": "PC游戏", "keyword": "怪物猎人"},
    {"name": "生化危机", "category": "游戏", "subcategory": "PC游戏", "keyword": "生化危机"},
    {"name": "战神", "category": "游戏", "subcategory": "PC游戏", "keyword": "战神"},
    {"name": "地平线", "category": "游戏", "subcategory": "PC游戏", "keyword": "地平线"},
]


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


def import_db(items, kw, category="软件", subcategory=""):
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
    log("通用资源爬虫 v1.0 启动")
    log("=" * 60)
    
    all_keywords = []
    
    # ── 1. 软件/工具 ──
    log("\n📡 [1/3] 软件/工具")
    for sw in SOFTWARE_LIST:
        all_keywords.append({
            "keyword": sw["keyword"],
            "category": sw["category"],
            "subcategory": sw["subcategory"],
            "source": "软件列表"
        })
    log(f"  软件列表: {len(SOFTWARE_LIST)} 个")
    
    # ── 2. 游戏 ──
    log("\n📡 [2/3] 游戏")
    for game in GAME_LIST:
        all_keywords.append({
            "keyword": game["keyword"],
            "category": game["category"],
            "subcategory": game["subcategory"],
            "source": "游戏列表"
        })
    log(f"  游戏列表: {len(GAME_LIST)} 个")
    
    # ── 3. GitHub Trending ──
    log("\n📡 [3/3] GitHub Trending")
    github = GitHubCrawler()
    github_items = github.fetch_topics("developer-tools", limit=20)
    github_items.extend(github.fetch_topics("database", limit=10))
    github_items.extend(github.fetch_topics("design-tools", limit=10))
    
    for item in github_items:
        all_keywords.append({
            "keyword": item["title"],
            "category": item["category"],
            "subcategory": item["subcategory"],
            "source": item["source"]
        })
    log(f"  GitHub: {len(github_items)} 个")
    
    log(f"\n📊 总计: {len(all_keywords)} 个关键词")
    log("=" * 60)
    
    # ── 搜索并导入 ──
    log("\n🔍 开始搜索资源...")
    total_imp = 0
    
    for i, kw_info in enumerate(all_keywords, 1):
        kw = kw_info["keyword"]
        category = kw_info["category"]
        subcategory = kw_info["subcategory"]
        
        if i % 10 == 0 or i == len(all_keywords):
            log(f"  进度: {i}/{len(all_keywords)}, 已导入: {total_imp}")
        
        items = search_api(kw)
        if items:
            n = import_db(items, kw, category, subcategory)
            total_imp += n
        time.sleep(0.3)
    
    log(f"\n{'=' * 60}")
    log(f"🎉 完成!")
    log(f"  关键词: {len(all_keywords)}")
    log(f"  新增资源: {total_imp}")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
