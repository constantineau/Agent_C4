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
from pydantic import BaseModel, Field, field_validator

from .db import pool

INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")


def strip_nul(v: Optional[str]) -> Optional[str]:
    """Drop NUL bytes from incoming text — Postgres rejects them in `text`.

    Some N2K devices emit UTF-16LE strings that reach Signal K as a NUL-interleaved
    run (`'W\\x00E\\x00D...'` for `WEDNESDAY`), so stripping recovers the intended
    value rather than discarding it. This matters out of proportion to how rare it is:
    a batch is one `executemany` in one transaction, so a single un-stripped reading
    used to abort the whole batch and silently lose every good reading beside it.
    """
    return v.replace("\x00", "") if v is not None else None


class Reading(BaseModel):
    """One (source, path) reading — the collect-everything unit."""
    time: datetime
    source: str
    path: str
    value: Optional[float] = None
    str_value: Optional[str] = None

    _no_nul = field_validator("source", "path", "str_value")(strip_nul)


class RawBatch(BaseModel):
    boat_id: str = "sr33"
    readings: list[Reading] = Field(..., min_length=1)


class AisObservation(BaseModel):
    """One raw AIS target observation as heard on the boat's bus (B951 Class B).

    The boat forwards only what it heard — identity + position + motion. Geometry
    (range/bearing/CPA/TCPA) is NOT sent: the agent recomputes it cloud-side against
    own-ship's freshest fix, in keeping with collect-everything (the boat is dumb)."""
    time: datetime
    mmsi: int
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    sog: Optional[float] = None   # kn
    cog: Optional[float] = None   # deg true

    # AIS static names arrive fixed-width padded and hit the same one-bad-row-kills-the-
    # batch path as /ingest/raw, so they get the same treatment.
    _no_nul = field_validator("name")(strip_nul)


class AisBatch(BaseModel):
    boat_id: str = "sr33"
    targets: list[AisObservation] = Field(..., min_length=1)


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


# The original wide-table `POST /ingest` (one column per channel → `telemetry`) is retired:
# the collect-everything `/ingest/raw` → `telemetry_raw` superseded it (migration 002) and
# nothing ever read the wide table back. Migration 006 drops it.


@app.post("/ingest/ais", dependencies=[Depends(require_token)])
def ingest_ais(batch: AisBatch):
    """Store raw AIS target observations. Geometry columns stay NULL — the agent's
    ais.py recomputes range/bearing/CPA/TCPA live against own-ship position."""
    rows = [(t.time, batch.boat_id, t.mmsi, t.name, t.lat, t.lon, t.sog, t.cog)
            for t in batch.targets]
    sql = ("INSERT INTO ais_targets (time, boat_id, mmsi, name, lat, lon, sog, cog) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
    return {"accepted": len(rows), "boat_id": batch.boat_id}


@app.post("/ingest/raw", dependencies=[Depends(require_token)])
def ingest_raw(batch: RawBatch):
    """Collect-everything endpoint: store every (source, path) reading verbatim."""
    rows = [(r.time, batch.boat_id, r.source, r.path, r.value, r.str_value)
            for r in batch.readings]
    sql = ("INSERT INTO telemetry_raw (time, boat_id, source, path, value, str_value) "
           "VALUES (%s, %s, %s, %s, %s, %s)")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
    return {"accepted": len(rows), "boat_id": batch.boat_id}
