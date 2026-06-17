# Built from repo root (context: .) so it can include the shared engine code + knowledge files.
# Same image runs on the VPS bench (vcan0) and the real Pi (can0) — the portability rule.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv DATA_SOURCE=onboard

COPY pi/engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Reuse the exact engine modules from the cloud agent package; knowledge/ is a sibling of app/
# so sails.py finds knowledge/sr33_speed_guide.md. polars_sr33.sql is the canonical polar source
# OnboardSource parses (no psycopg, no LLM in this image).
COPY vps/agent/app/ ./app/
COPY shared/ ./shared/
COPY vps/agent/knowledge/ ./knowledge/
COPY vps/db/seed/polars_sr33.sql ./polars_sr33.sql
COPY pi/engine/engine_app.py ./engine_app.py

EXPOSE 8200
CMD ["uvicorn", "engine_app:app", "--host", "0.0.0.0", "--port", "8200"]
