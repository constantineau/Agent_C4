# Built from repo root (context: .) so it can include shared/.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

COPY vps/agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY vps/agent/knowledge/ ./knowledge/
# each boat's sail crossover model (its per-TWS toss-up overlaps feed the /sail dial), read by the
# boat id this agent runs for (knowledge/<BOAT_ID>_crossovers.json, #304); canonical copies in db/seed
COPY vps/db/seed/*_crossovers.json ./knowledge/
# each boat's bundled profile (#306): the coach names the boat it runs for (knowledge/boats/<BOAT_ID>.json)
COPY vps/lab/boats/ ./knowledge/boats/
COPY vps/agent/app/ ./app/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
