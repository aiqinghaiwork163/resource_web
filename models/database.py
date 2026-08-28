"""MySQL connection pool (lazy initialization)."""
import pymysql
from dbutils.pooled_db import PooledDB

from config import DB_CONFIG

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = PooledDB(
            creator=pymysql,
            maxconnections=20,
            mincached=2,
            maxcached=5,
            blocking=True,
            maxusage=None,
            setsession=["SET NAMES utf8mb4"],
            ping=1,
            **DB_CONFIG,
        )
    return _pool


def get_db():
    conn = _get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET time_zone = '+08:00'")
    except Exception:
        pass
    return conn
