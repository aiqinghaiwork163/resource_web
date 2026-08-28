"""TMDB API helpers."""
import os

from models.database import get_db
def get_tmdb_api_key():
    """从 settings 表中获取 TMDB API Key，环境变量 TMDB_API_KEY 优先"""
    env_key = os.environ.get("TMDB_API_KEY")
    if env_key:
        return env_key
    try:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT `value` FROM settings WHERE `key`='tmdb_api_key'")
            row = cur.fetchone()
            if row and row.get("value"):
                return row["value"]
        finally:
            db.close()
    except Exception:
        pass
    raise RuntimeError("TMDB API Key 未配置：请设置环境变量 TMDB_API_KEY 或在 settings 表中添加 tmdb_api_key")
