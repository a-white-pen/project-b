"""
Application entry point — creates the single FastAPI instance and wires all inbound route modules.

To add a new inbound source: import its register_routes and add one call inside create_app().

Functions:
  create_app() — builds the FastAPI app and registers all active inbound route modules
  app          — module-level FastAPI instance; used as the uvicorn entry point (app:app)
"""

from fastapi import FastAPI

from system.logging import configure_logging

configure_logging()

from inbound.strava.webhook import register_routes as register_strava_routes

# Garmin inbound — waiting to build (Phase 3)
# from inbound.garmin.webhook import register_routes as register_garmin_routes

# Gmail inbound — waiting to build (future)
# from inbound.gmail.webhook import register_routes as register_gmail_routes

from telegram.webhook import register_routes as register_telegram_routes


def create_app() -> FastAPI:
    # Creates the shared FastAPI instance and registers every inbound route module onto it.
    # One register_routes() call per source — this is the full list of active inbound channels.
    app = FastAPI(title="project-b", docs_url=None, redoc_url=None)
    register_telegram_routes(app)
    register_strava_routes(app)
    # register_garmin_routes(app)   # Phase 3 — waiting to build
    # register_gmail_routes(app)    # future — waiting to build
    return app


app = create_app()
