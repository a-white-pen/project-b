"""
Database connection management using psycopg2.

Functions:
  get_connection() — returns a live psycopg2 connection from DATABASE_URL
"""

import os
import psycopg2
import psycopg2.extensions


# Opens and returns a new psycopg2 connection. Caller is responsible for closing it.
# Uses DATABASE_URL from environment; strips trailing newline from Secret Manager values.
def get_connection() -> psycopg2.extensions.connection:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(url)
