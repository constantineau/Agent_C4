"""TimescaleDB connection pool for the agent's read tools (psycopg3)."""
import os
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://sr33:sr33-dev@timescaledb:5432/sr33_dev"
)

pool = ConnectionPool(
    DATABASE_URL, min_size=1, max_size=5, open=False,
    kwargs={"row_factory": dict_row},
)
