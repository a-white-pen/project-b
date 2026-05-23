"""
Application entry point — creates the single FastAPI instance and wires all route modules.

To add a new inbound source: import its register_routes and add one call inside create_app().
To add a new public API: import its register_routes from api/ and add a call inside create_app().

Functions:
  create_app() — builds the FastAPI app, attaches rate limiter, and registers all route modules
  app          — module-level FastAPI instance; used as the uvicorn entry point (app:app)
"""

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from system.logging import configure_logging

configure_logging()

from api.data_visualisation import register_routes as register_data_vis_routes
from api.limiter import limiter
from api.menus import register_routes as register_menus_routes
from inbound.strava.webhook import register_routes as register_strava_routes

# Garmin inbound — waiting to build (Phase 3)
# from inbound.garmin.webhook import register_routes as register_garmin_routes

# Gmail inbound — waiting to build (future)
# from inbound.gmail.webhook import register_routes as register_gmail_routes

from telegram.webhook import register_routes as register_telegram_routes


def create_app() -> FastAPI:
    # Creates the shared FastAPI instance, attaches the rate limiter, and registers every
    # route module. One register_routes() call per source or API surface.
    app = FastAPI(title="project-b", docs_url=None, redoc_url=None)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_telegram_routes(app)
    register_strava_routes(app)
    register_data_vis_routes(app)
    register_menus_routes(app)
    # register_garmin_routes(app)   # Phase 3 — waiting to build
    # register_gmail_routes(app)    # future — waiting to build
    return app


app = create_app()
