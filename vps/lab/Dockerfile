# Built from repo root (context: .) so it can include shared/ + the bundled race library.
# Serves both the Lab web shell (static) and the race-library API from one container.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv \
    RACES_DIR=/srv/races BOATS_DIR=/srv/boats INGESTED_DIR=/srv/ingested WEB_DIR=/srv/web \
    BOAT_DATA_DIR=/srv/boatdata \
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
# the forecast-plume chart is the console's own drawing code — one copy, two surfaces
COPY pi/console/dashboard/plume.js ./web/plume.js
COPY vps/lab/races/ ./races/
COPY vps/lab/boats/ ./boats/
# Boat data: each boat profile names its OWN polar files (`polar_files`, #303) and they are looked
# up here — the ORC polar envelope, per-sail curves (2g), the crossover table (2b) and the speed
# guide (sent to the boat at Deploy). No boat's files are a default: a profile that names none
# routes on its inline cert polar or not at all.
COPY vps/db/seed/polars_*.sql vps/db/seed/*_sail_polars.json vps/db/seed/*_crossovers.json \
     vps/agent/knowledge/*_speed_guide.md ./boatdata/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
