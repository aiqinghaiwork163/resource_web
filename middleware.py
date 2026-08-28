"""HTTP middleware registration."""
import gzip
import io
import time
from collections import OrderedDict

from flask import request


def register_middleware(app):
    @app.after_request
    def add_gzip(response):
        try:
            if response.status_code != 200:
                return response
            if response.headers.get("Content-Encoding"):
                return response
            accept = request.headers.get("Accept-Encoding", "")
            if "gzip" not in accept:
                return response
            ct = response.content_type or ""
            if not any(t in ct for t in ["json", "html", "javascript", "css", "text"]):
                return response
            data = response.get_data()
            if len(data) < 500:
                return response
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as f:
                f.write(data)
            response.set_data(buf.getvalue())
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = len(response.get_data())
            response.headers["Vary"] = "Accept-Encoding"
        except (RuntimeError, TypeError):
            pass
        return response

    @app.after_request
    def cache_static(response):
        if request.path.startswith("/api/") or request.path.startswith("/login"):
            return response
        if response.status_code == 200 and not response.headers.get("Cache-Control"):
            response.headers["Cache-Control"] = "public, max-age=300"
        return response

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response
