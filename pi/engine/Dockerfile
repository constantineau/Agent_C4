# Built from repo root (context: .) so it can include the shared engine code.
# Same image runs on the VPS bench (vcan0) and the real Pi (can0) — the portability rule.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv DATA_SOURCE=onboard

COPY pi/engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Reuse the exact engine modules from the cloud agent package (no psycopg, no LLM in this image).
# No boat data in the image (#304): the polar, speed guide and sail crossovers are the DEPLOYED
# boat's, carried by the Lab's Deploy push with its profile (POST /reroute/assets). Nothing
# deployed → the engine says "No boat polar loaded.", never another boat's numbers.
COPY vps/agent/app/ ./app/
COPY shared/ ./shared/
COPY pi/engine/engine_app.py ./engine_app.py

EXPOSE 8200
# No access log (#184-1): the iPad polls every few seconds, so it is one line per poll on the
# archive's SD card and drowns the real errors. Errors and warnings still log.
CMD ["uvicorn", "engine_app:app", "--host", "0.0.0.0", "--port", "8200", "--no-access-log"]
