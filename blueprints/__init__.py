"""Route blueprints split from app.py (P2-10)."""
from .frontend import frontend_bp
from .auth import auth_bp
from .admin import admin_bp
from .crawler import crawler_bp

__all__ = ['frontend_bp','auth_bp','admin_bp','crawler_bp']
