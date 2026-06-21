# Built from repo root (context: .) so it can include shared/ + the bundled race library.
# Serves both the Lab web shell (static) and the race-library API from one container.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv \
    RACES_DIR=/srv/races BOATS_DIR=/srv/boats INGESTED_DIR=/srv/ingested WEB_DIR=/srv/web \
    POLARS_FILE=/srv/polars_sr33.sql CROSSOVERS_FILE=/srv/sr33_crossovers.json \
    GRIB_CACHE=/srv/gribcache ENC_CACHE=/srv/enc

# gdal-bin = the S-57 driver (ogr2ogr) used at PREP time to turn NOAA ENC charts into GeoJSON
# (app/geo/enc.py). The routing hot loop stays pure-python; GDAL is only invoked when prepping a bbox.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdal-bin \
    && rm -rf /var/lib/apt/lists/*

COPY vps/lab/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m eccodes selfcheck

COPY shared/ ./shared/
COPY vps/lab/app/ ./app/
COPY vps/lab/web/ ./web/
COPY vps/lab/races/ ./races/
COPY vps/lab/boats/ ./boats/
# the one canonical ORC polar (same source the onboard engine parses) for the optimizer
COPY vps/db/seed/polars_sr33.sql ./polars_sr33.sql
# the sail crossover model (per-TWS sail zones) for routing-fidelity 2b — per-leg sail plan
COPY vps/db/seed/sr33_crossovers.json ./sr33_crossovers.json

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
