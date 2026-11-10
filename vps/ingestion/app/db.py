"""Thin connection-pool helper around TimescaleDB (psycopg3)."""
import os
from psycopg_pool import ConnectionPool

# No default DSN (no password in the repo). The service's main.py refuses to start without it;
# a module that only imports this (the Pi engine shares agent code) never opens the pool.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Opened lazily; FastAPI lifespan (see main.py) waits for the DB to be reachable.
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False)
