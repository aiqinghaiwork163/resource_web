"""Redis cache helpers."""
import hashlib
import json
import os

_redis_client = None


def get_redis():
    global _redis_client
    if _redis_client is None:
        import redis

        _redis_client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            db=int(os.environ.get("REDIS_DB", "0")),
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _redis_client


def make_cache_key(*args):
    raw = "|".join(str(a) for a in args)
    return "res_web:" + hashlib.md5(raw.encode()).hexdigest()


def invalidate_resource_cache():
    try:
        for key in get_redis().scan_iter("res_web:resources:*"):
            get_redis().delete(key)
    except Exception:
        pass
