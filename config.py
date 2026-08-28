"""Application configuration from environment variables."""
import os

import pymysql.cursors

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "172.23.0.2"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD"),
    "database": os.environ.get("DB_NAME", "pan_resource"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "connect_timeout": 5,
    "read_timeout": 10,
    "write_timeout": 10,
}

ADMIN_USER = os.environ.get("ADMIN_USER")
ADMIN_PASS = os.environ.get("ADMIN_PASS")


def validate_config(secret_key: str | None) -> None:
    if not ADMIN_USER or not ADMIN_PASS or not secret_key:
        raise RuntimeError("环境变量 ADMIN_USER、ADMIN_PASS 和 SECRET_KEY 必须设置")
    if not os.environ.get("DB_PASSWORD"):
        raise RuntimeError("环境变量 DB_PASSWORD 必须设置")
