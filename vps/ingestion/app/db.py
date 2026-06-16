"""Thin connection-pool helper around TimescaleDB (psycopg3)."""
import os
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://sr33:sr33-dev@timescaledb:5432/sr33_dev"
)

# Opened lazily; FastAPI lifespan (see main.py) waits for the DB to be reachable.
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False)
