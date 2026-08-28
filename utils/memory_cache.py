"""In-memory LRU-ish cache used by decorators and TMDB."""
import time
from functools import wraps

from flask import request

memory_cache: dict = {}
_CACHE_MAX_SIZE = 500


def cleanup_cache():
    now = time.time()
    expired = [k for k, (_, expire) in memory_cache.items() if now >= expire]
    for k in expired:
        memory_cache.pop(k, None)
    if len(memory_cache) > _CACHE_MAX_SIZE:
        keep = list(memory_cache.items())[-_CACHE_MAX_SIZE // 2 :]
        memory_cache.clear()
        memory_cache.update(keep)


def cached(ttl=300):
    """Simple in-memory cache decorator keyed by request URL."""

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = request.url
            now = time.time()
            if len(memory_cache) > _CACHE_MAX_SIZE:
                cleanup_cache()
            if key in memory_cache:
                data, expire = memory_cache[key]
                if now < expire:
                    return data
            result = f(*args, **kwargs)
            memory_cache[key] = (result, now + ttl)
            return result

        return wrapper

    return decorator


def invalidate_url_cache_keys():
    for k in list(memory_cache.keys()):
        if "/api/" in k:
            del memory_cache[k]
