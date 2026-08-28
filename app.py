#!/usr/bin/env python3
"""
网盘资源浏览 Web 应用 v4.0
Flask 应用工厂 + Blueprint 模块化架构
"""
import os

from flask import Flask

from config import validate_config
from extensions import csrf
from middleware import register_middleware
from routes.register import register_blueprints


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["WTF_CSRF_HEADERS"] = ["X-CSRFToken", "X-CSRF-Token"]

    validate_config(app.secret_key)

    csrf.init_app(app)
    register_middleware(app)
    register_blueprints(app)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
