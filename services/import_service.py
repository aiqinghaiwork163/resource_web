"""Auto-import background processing."""
import threading
import time

import requests as http_requests

from models.database import get_db
from services.link_checker import check_single_link
from utils.redis_cache import invalidate_resource_cache
def auto_import_trigger(keyword):
    """当搜索无结果时，自动加入队列并立即后台处理。返回值: True=导入中, 'exhausted'=已尝试过无结果, False=不触发"""
    try:
        db = get_db()
        cur = db.cursor()
        # 检查是否已在队列中（正在处理）
        cur.execute(
            "SELECT id, status FROM import_queue WHERE keyword = %s AND status IN ('pending','processing')",
            (keyword,)
        )
        if cur.fetchone():
            db.close()
            return True  # 正在处理中
        # 检查之前是否已经导入过（2次以上且全部0结果）
        cur.execute(
            "SELECT COUNT(*) as cnt, SUM(result_count) as total FROM import_queue WHERE keyword = %s AND status = 'done'",
            (keyword,)
        )
        row = cur.fetchone()
        if row and row["cnt"] >= 2 and (row["total"] or 0) == 0:
            db.close()
            return "exhausted"  # 已尝试多次，没有资源
        # 检查自动导入是否开启
        cur.execute("SELECT value FROM settings WHERE `key`='auto_import_enabled'")
        row = cur.fetchone()
        if not row or row["value"] != "1":
            db.close()
            return False
        # 加入队列
        cur.execute(
            "INSERT INTO import_queue (keyword, status) VALUES (%s, 'processing')",
            (keyword,)
        )
        qid = cur.lastrowid
        db.commit()
        db.close()
        # 后台线程立即处理
        import threading
        t = threading.Thread(target=process_import, args=(keyword, qid), daemon=True)
        t.start()
        return True
    except Exception:
        return False

def auto_check_links_bg(items):
    """后台线程：批量检测新导入资源的链接有效性"""
    import threading
    def _worker(items):
        for item in items:
            try:
                url = item.get("url") or ""
                source = (item.get("source") or "").lower()
                rid = item.get("id")
                if not url or not rid:
                    continue
                status, code, msg = check_single_link(url, source)
                db = get_db()
                try:
                    cur = db.cursor()
                    cur.execute("UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s", (status, rid))
                    db.commit()
                    invalidate_resource_cache()
                finally:
                    db.close()
            except:
                pass
    t = threading.Thread(target=_worker, args=(items,), daemon=True)
    t.start()

def auto_scan_unchecked_bg(limit=50):
    """同步扫描未检测的资源链接（在调用方线程中执行，不新开线程）"""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "SELECT id, source, url FROM resources "
            "WHERE source NOT IN ('magnet','ed2k','other') AND last_checked IS NULL "
            "ORDER BY id DESC LIMIT %s", (limit,)
        )
        items = cur.fetchall()
        db.close()
        for item in items:
            try:
                url = item.get("url") or ""
                source = (item.get("source") or "").lower()
                rid = item.get("id")
                if not url or not rid:
                    continue
                status, code, msg = check_single_link(url, source)
                try:
                    db2 = get_db()
                    cur2 = db2.cursor()
                    cur2.execute("UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s", (status, rid))
                    db2.commit()
                    db2.close()
                except:
                    pass
            except:
                pass
    except:
        pass

def fetch_api(api_url, keyword, limit):
    """从单个API搜索资源"""
    import requests as http_requests
    try:
        resp = http_requests.post(api_url, json={"kw": keyword, "limit": limit}, timeout=60)
        data = resp.json()
        if data.get("code") != 0:
            return []
        merged = data.get("data", {}).get("merged_by_type", {})
        results = []
        for source, items in merged.items():
            for item in items:
                results.append({"source": source, "item": item})
        return results
    except Exception:
        return []

def process_import(keyword, queue_id):
    """后台处理单个导入任务（支持多API并发）"""
    import threading
    try:
        db = get_db()
        cur = db.cursor()
        # 获取API配置（支持多个，换行分隔）
        cur.execute("SELECT value FROM settings WHERE `key`='import_api_url'")
        row = cur.fetchone()
        api_urls_raw = row["value"] if row else "https://ps.aiqinghaiwork.cn/api/search"
        api_urls = [u.strip() for u in api_urls_raw.split("\n") if u.strip()]
        if not api_urls:
            api_urls = [api_urls_raw]

        cur.execute("SELECT value FROM settings WHERE `key`='import_api_limit'")
        row = cur.fetchone()
        limit = int(row["value"]) if row else 30
        db.close()

        # 并发请求所有API
        all_results = []
        if len(api_urls) == 1:
            all_results = fetch_api(api_urls[0], keyword, limit)
        else:
            threads = []
            results_map = {}
            def fetch_one(url, idx):
                results_map[idx] = fetch_api(url, keyword, limit)
            for i, url in enumerate(api_urls):
                t = threading.Thread(target=fetch_one, args=(url, i))
                threads.append(t)
                t.start()
            for t in threads:
                t.join(timeout=90)
            for i in range(len(api_urls)):
                all_results.extend(results_map.get(i, []))

        # 去重（按URL）
        seen_urls = set()
        unique_results = []
        for r in all_results:
            url = r["item"].get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_results.append(r)

        # 写入数据库
        db = get_db()
        try:
            cur = db.cursor()
            imported = 0
            for r in unique_results:
                item = r["item"]
                source = r["source"]
                cur.execute("""INSERT IGNORE INTO resources
                    (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                    (keyword, source, item.get("url", ""), item.get("note", "") or item.get("title", ""),
                     item.get("note", ""), item.get("password", ""), item.get("datetime", ""), "", "", 0))
                imported += 1

            cur.execute("UPDATE import_queue SET status='done', result_count=%s WHERE id=%s",
                       (imported, queue_id))
            cur.execute("UPDATE search_logs SET is_imported=1 WHERE keyword=%s", (keyword,))
            db.commit()
        finally:
            db.close()

        # 自动检测新导入资源的链接有效性
        if imported > 0:
            auto_scan_unchecked_bg(min(imported, 50))
    except Exception as e:
        try:
            db = get_db()
            try:
                cur = db.cursor()
                cur.execute("UPDATE import_queue SET status='failed', error_msg=%s WHERE id=%s",
                           (str(e)[:200], queue_id))
                db.commit()
            finally:
                db.close()
        except:
            pass
