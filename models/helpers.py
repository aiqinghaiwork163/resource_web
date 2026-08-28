"""Shared model/query helpers."""
import time

from models.database import get_db
from utils.memory_cache import memory_cache
from utils.redis_cache import invalidate_resource_cache

_FW_CACHE_KEY = "_filter_words"
_FW_CACHE_TTL = 300
def merge_source(src):
    """把 plugin:xxx 归类到主网盘类型"""
    if src.startswith("plugin:"):
        return "other"
    return src

def get_cached_filter_words():
    """从内存缓存获取filter_words列表，TTL 300秒"""
    now = time.time()
    if _FW_CACHE_KEY in memory_cache:
        data, expire = memory_cache[_FW_CACHE_KEY]
        if now < expire:
            return data
    # 缓存过期，从DB加载
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT word FROM filter_words")
        fwords = [r["word"] for r in cur.fetchall()]
    finally:
        db.close()
    memory_cache[_FW_CACHE_KEY] = (fwords, now + _FW_CACHE_TTL)
    return fwords

def clear_filter_words_cache():
    """清除filter_words缓存"""
    memory_cache.pop(_FW_CACHE_KEY, None)
    invalidate_resource_cache()

def filter_words_condition(cur):
    """返回 (sql_fragment, params) 用于排除含过滤词的资源（内存缓存300秒）"""
    fwords = get_cached_filter_words()
    if not fwords:
        return "", []
    conds = []
    params = []
    for w in fwords:
        conds.append("(title NOT LIKE %s AND note NOT LIKE %s AND keyword NOT LIKE %s)")
        params.extend([f"%{w}%", f"%{w}%", f"%{w}%"])
    return "(" + " AND ".join(conds) + ")", params
