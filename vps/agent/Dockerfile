# Built from repo root (context: .) so it can include shared/.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

COPY vps/agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY vps/agent/knowledge/ ./knowledge/
# the sail crossover model (its per-TWS toss-up overlaps feed the /sail dial); canonical copy in db/seed
COPY vps/db/seed/sr33_crossovers.json ./knowledge/sr33_crossovers.json
COPY vps/agent/app/ ./app/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
