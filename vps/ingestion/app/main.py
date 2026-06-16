"""Agent_C4 ingestion API.

One token-authenticated endpoint that accepts telemetry batches from the Pi uplink and
writes them to TimescaleDB. Deliberately tiny — the boat is the source of truth; this
just durably records what it sends.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from shared.units import TELEMETRY_CHANNELS
from .db import pool

INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")


class TelemetryPoint(BaseModel):
    time: datetime
    aws: Optional[float] = None
    awa: Optional[float] = None
    tws: Optional[float] = None
    twa: Optional[float] = None
    twd: Optional[float] = None
    stw: Optional[float] = None
    sog: Optional[float] = None
    cog: Optional[float] = None
    heading: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    depth: Optional[float] = None


class TelemetryBatch(BaseModel):
    boat_id: str = "sr33"
    points: list[TelemetryPoint] = Field(..., min_length=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True, timeout=30)
    yield
    pool.close()


app = FastAPI(title="Agent_C4 Ingestion API", version="0.1.0", lifespan=lifespan)


def require_token(authorization: str = Header(default="")):
    expected = f"Bearer {INGEST_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing ingest token")


@app.get("/health")
def health():
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "db": "up"}
    except Exception as exc:  # pragma: no cover - surfaced to caller
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}")


_COLS = ("time", "boat_id", *TELEMETRY_CHANNELS)


@app.post("/ingest", dependencies=[Depends(require_token)])
def ingest(batch: TelemetryBatch):
    rows = []
    for p in batch.points:
        d = p.model_dump()
        rows.append((d["time"], batch.boat_id, *(d[c] for c in TELEMETRY_CHANNELS)))

    placeholders = "(" + ", ".join(["%s"] * len(_COLS)) + ")"
    sql = f"INSERT INTO telemetry ({', '.join(_COLS)}) VALUES {placeholders}"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
    return {"accepted": len(rows), "boat_id": batch.boat_id}
