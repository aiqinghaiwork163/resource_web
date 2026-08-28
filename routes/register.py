"""Register all Flask blueprints."""
from routes.admin.crawlers import admin_bp  # noqa: F401 - registers via misc
from routes.admin.deadlinks import admin_bp  # noqa: F401
from routes.admin.filter_words import admin_bp  # noqa: F401
from routes.admin.import_queue import admin_bp  # noqa: F401
from routes.admin.logs import admin_bp  # noqa: F401
from routes.admin.misc import admin_bp
from routes.admin.resources import admin_bp  # noqa: F401
from routes.admin.settings import admin_bp  # noqa: F401
from routes.admin.stats import admin_bp  # noqa: F401
from routes.api import api_bp
from routes.auth import auth_bp
from routes.pages import pages_bp
from routes.tmdb import tmdb_bp


def register_blueprints(app):
    app.register_blueprint(pages_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(tmdb_bp)
    app.register_blueprint(admin_bp)
