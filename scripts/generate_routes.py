#!/usr/bin/env python3
"""Generate route/service modules from _legacy_app.py using AST line ranges."""
from __future__ import annotations

import ast
import os
import re

ROOT = "/workspace"
SRC = os.path.join(ROOT, "_legacy_app.py")

# name -> (blueprint variable name, output file, public name or None)
ASSIGNMENTS: dict[str, tuple[str, str, str | None]] = {}

def route(name, bp, path, public=None):
    ASSIGNMENTS[name] = (bp, path, public)

def svc(name, path, public=None):
    ASSIGNMENTS[name] = ("", path, public or name.lstrip("_"))

# --- routes ---
for n in ["login_page","logout","index","admin_page","profile_page","api_docs_page","site_settings_js","favicon"]:
    route(n, "pages_bp", "routes/pages.py")
for n in ["api_user_me","api_me","api_change_password","api_users","api_user_update","api_user_delete","api_user_add"]:
    route(n, "auth_bp", "routes/auth.py")
for n in [
    "api_site_settings","api_stats","api_src_tags","api_resources","api_sources","api_keywords","api_import",
    "api_csrf_token","api_health","api_favorites","api_fav_add","api_fav_del","api_search_hist","api_search_hist_add",
    "api_search_hist_clear","api_click","api_search_log","api_hot_searches","api_autocomplete","api_resource_detail",
    "api_recommendations","api_subscriptions","api_subscription_add","api_subscription_del","api_resource_share",
    "api_shared_resource","api_import_status","api_docs",
]:
    route(n, "api_bp", "routes/api.py")
for n in ["img_proxy","api_tmdb_trending"]:
    route(n, "tmdb_bp", "routes/tmdb.py")
for n in ["api_pending_imports","api_confirm_import","api_admin_test_apis"]:
    route(n, "admin_bp", "routes/admin/misc.py")
for n in ["api_operation_logs","api_admin_logs_clear"]:
    route(n, "admin_bp", "routes/admin/logs.py")
for n in ["api_filter_words","api_filter_words_add","api_filter_words_del"]:
    route(n, "admin_bp", "routes/admin/filter_words.py")
route("api_admin_dashboard", "admin_bp", "routes/admin/stats.py")
for n in ["api_admin_settings","api_admin_settings_update"]:
    route(n, "admin_bp", "routes/admin/settings.py")
for n in ["api_admin_resources_batch","api_admin_resources"]:
    route(n, "admin_bp", "routes/admin/resources.py")
for n in ["api_admin_import_queue","api_admin_import_retry","api_admin_import_queue_delete","api_admin_process_queue"]:
    route(n, "admin_bp", "routes/admin/import_queue.py")
for n in [
    "api_admin_deadlinks_scan","api_admin_deadlinks_delete","api_admin_deadlinks_sources",
    "api_admin_deadlinks_unchecked_count","api_admin_deadlinks_auto_scan",
]:
    route(n, "admin_bp", "routes/admin/deadlinks.py")
for n in ["api_tmdb_crawl","api_tmdb_status","api_multi_crawl","api_multi_crawl_status","api_omdb_crawl","api_omdb_status","_tmdb_crawler_worker"]:
    route(n, "admin_bp", "routes/admin/crawlers.py")

# --- services / helpers ---
svc("_merge_source", "models/helpers.py", "merge_source")
svc("_get_cached_filter_words", "models/helpers.py", "get_cached_filter_words")
svc("_clear_filter_words_cache", "models/helpers.py", "clear_filter_words_cache")
svc("_filter_words_condition", "models/helpers.py", "filter_words_condition")
svc("_auto_import_trigger", "services/import_service.py", "auto_import_trigger")
svc("_auto_check_links_bg", "services/import_service.py", "auto_check_links_bg")
svc("_auto_scan_unchecked_bg", "services/import_service.py", "auto_scan_unchecked_bg")
svc("_fetch_api", "services/import_service.py", "fetch_api")
svc("_process_import", "services/import_service.py", "process_import")
svc("_log_op", "services/operation_log.py", "log_op")
svc("_extract_id", "services/link_checker.py", "extract_id")
svc("_check_single_link", "services/link_checker.py", "check_single_link")
svc("_get_tmdb_api_key", "services/tmdb_service.py", "get_tmdb_api_key")

HEADERS: dict[str, str] = {
    "routes/pages.py": '''"""HTML page routes."""
from flask import Blueprint, jsonify, make_response, redirect, request, send_from_directory, session

from utils.auth import admin_required

pages_bp = Blueprint("pages", __name__)
''',
    "routes/auth.py": '''"""Authentication and user management."""
import pymysql
from flask import Blueprint, jsonify, redirect, request, send_from_directory, session

from models.database import get_db
from utils.auth import (
    admin_required,
    check_login_limit,
    hash_password,
    record_login_failure,
    reset_login_failures,
    verify_password,
)

auth_bp = Blueprint("auth", __name__)
''',
    "routes/api.py": '''"""Public REST API routes."""
import html as html_mod
import json
import time
from datetime import datetime

import pymysql
import requests as http_requests
from flask import Blueprint, current_app, jsonify, request, session
from flask_wtf.csrf import generate_csrf

from models.database import get_db
from models.helpers import filter_words_condition, get_cached_filter_words, merge_source
from services.import_service import auto_import_trigger
from services.operation_log import log_op
from utils.auth import admin_required, hash_password, verify_password
from utils.memory_cache import memory_cache, invalidate_url_cache_keys
from utils.rate_limit import check_rate_limit
from utils.redis_cache import get_redis, invalidate_resource_cache, make_cache_key

api_bp = Blueprint("api", __name__)
''',
    "routes/tmdb.py": '''"""TMDB and image proxy routes."""
import json
import socket
import ssl as _ssl
import time
import urllib.parse
from collections import OrderedDict

import requests as _req
from flask import Blueprint, jsonify, request

from models.database import get_db
from services.tmdb_service import get_tmdb_api_key
from utils.memory_cache import memory_cache

tmdb_bp = Blueprint("tmdb", __name__)

_img_cache: OrderedDict = OrderedDict()
_IMG_CACHE_MAX = 1000


def img_cache_evict():
    while len(_img_cache) > _IMG_CACHE_MAX:
        _img_cache.popitem(last=False)
''',
    "routes/admin/misc.py": '''"""Miscellaneous admin API routes."""
import json
import time

import requests as http_requests
from flask import Blueprint, jsonify, request, session

from models.database import get_db
from services.operation_log import log_op
from utils.auth import admin_required

admin_bp = Blueprint("admin", __name__)
''',
    "routes/admin/logs.py": '''"""Admin operation logs."""
from flask import jsonify, request

from models.database import get_db
from utils.auth import admin_required

from routes.admin.misc import admin_bp
''',
    "routes/admin/filter_words.py": '''"""Admin filter word management."""
from flask import jsonify, request

from models.database import get_db
from models.helpers import clear_filter_words_cache
from utils.auth import admin_required

from routes.admin.misc import admin_bp
''',
    "routes/admin/stats.py": '''"""Admin dashboard statistics."""
from flask import jsonify

from models.database import get_db
from utils.auth import admin_required

from routes.admin.misc import admin_bp
''',
    "routes/admin/settings.py": '''"""Admin system settings."""
from flask import jsonify, request

from models.database import get_db
from utils.auth import admin_required
from utils.memory_cache import invalidate_url_cache_keys

from routes.admin.misc import admin_bp
''',
    "routes/admin/resources.py": '''"""Admin resource management."""
from flask import jsonify, request

from models.database import get_db
from utils.auth import admin_required
from utils.redis_cache import invalidate_resource_cache

from routes.admin.misc import admin_bp
''',
    "routes/admin/import_queue.py": '''"""Admin import queue management."""
import requests as http_requests

from flask import jsonify, request

from models.database import get_db
from services.import_service import auto_scan_unchecked_bg
from utils.auth import admin_required
from utils.memory_cache import invalidate_url_cache_keys

from routes.admin.misc import admin_bp
''',
    "routes/admin/deadlinks.py": '''"""Admin dead link scanning."""
import threading

from flask import jsonify, request

from models.database import get_db
from services.link_checker import check_single_link
from utils.auth import admin_required
from utils.redis_cache import invalidate_resource_cache

from routes.admin.misc import admin_bp
''',
    "routes/admin/crawlers.py": '''"""Admin crawler controls."""
import json
import subprocess
from datetime import datetime

from flask import jsonify

from services.tmdb_service import get_tmdb_api_key
from utils.auth import admin_required

from routes.admin.misc import admin_bp

_tmdb_crawler_status = {"running": False, "progress": 0, "total": 0, "imported": 0, "log": []}
_multi_crawler_status = {"running": False, "phase": "", "progress": 0, "total": 0, "imported": 0, "log": []}
_omdb_crawler_status = {"running": False, "progress": 0, "total": 0, "success": 0, "failed": 0, "log": []}
''',
    "models/helpers.py": '''"""Shared model/query helpers."""
import time

from models.database import get_db
from utils.memory_cache import memory_cache
from utils.redis_cache import invalidate_resource_cache

_FW_CACHE_KEY = "_filter_words"
_FW_CACHE_TTL = 300
''',
    "services/import_service.py": '''"""Auto-import background processing."""
import threading
import time

import requests as http_requests

from models.database import get_db
from services.link_checker import check_single_link
from utils.redis_cache import invalidate_resource_cache
''',
    "services/operation_log.py": '''"""Operation audit logging."""
from flask import request

from models.database import get_db
''',
    "services/link_checker.py": '''"""Cloud drive link validity checks."""
import re

import requests as http_requests
''',
    "services/tmdb_service.py": '''"""TMDB API helpers."""
import os

from models.database import get_db
''',
}

RENAME = {
    "_merge_source": "merge_source",
    "_get_cached_filter_words": "get_cached_filter_words",
    "_clear_filter_words_cache": "clear_filter_words_cache",
    "_filter_words_condition": "filter_words_condition",
    "_auto_import_trigger": "auto_import_trigger",
    "_auto_check_links_bg": "auto_check_links_bg",
    "_auto_scan_unchecked_bg": "auto_scan_unchecked_bg",
    "_fetch_api": "fetch_api",
    "_process_import": "process_import",
    "_log_op": "log_op",
    "_extract_id": "extract_id",
    "_check_single_link": "check_single_link",
    "_get_tmdb_api_key": "get_tmdb_api_key",
    "_make_cache_key": "make_cache_key",
    "_invalidate_resource_cache": "invalidate_resource_cache",
    "_img_cache_evict": "img_cache_evict",
}


def get_decorator_start(lines: list[str], func_line: int) -> int:
    i = func_line - 2
    while i >= 0:
        stripped = lines[i].strip()
        if stripped.startswith("@"):
            i -= 1
            continue
        if stripped == "" or stripped.startswith("#"):
            i -= 1
            continue
        break
    return i + 1


def transform(text: str, bp: str, public_name: str | None) -> str:
    if bp:
        text = text.replace("@app.route", f"@{bp}.route")
    if public_name and public_name != text.split("def ")[1].split("(")[0]:
        text = re.sub(r"^def \w+", f"def {public_name}", text, count=1, flags=re.MULTILINE)
    for old, new in RENAME.items():
        text = text.replace(old, new)
    text = re.sub(r"(?<![\w.])_cache(?![\w.])", "memory_cache", text)
    text = text.replace("memory_cache_key", "_FW_CACHE_KEY")
    return text


def main():
    with open(SRC, encoding="utf-8") as f:
        lines = f.readlines()
    source = "".join(lines)
    tree = ast.parse(source)

    buckets: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in ASSIGNMENTS:
            continue
        bp, path, public = ASSIGNMENTS[node.name]
        start = get_decorator_start(lines, node.lineno)
        chunk = "".join(lines[start:node.end_lineno])
        chunk = transform(chunk, bp, public)
        buckets.setdefault(path, []).append(chunk.rstrip())

    for path, chunks in buckets.items():
        out = os.path.join(ROOT, path)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        header = HEADERS.get(path, "")
        with open(out, "w", encoding="utf-8") as f:
            f.write(header)
            f.write("\n\n".join(chunks))
            f.write("\n")
        print(f"Wrote {path}: {len(chunks)} functions")


if __name__ == "__main__":
    main()
