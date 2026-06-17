# Built from repo root (context: .) so it can include shared/ + the bundled race library.
# Serves both the Lab web shell (static) and the race-library API from one container.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv \
    RACES_DIR=/srv/races INGESTED_DIR=/srv/ingested WEB_DIR=/srv/web

COPY vps/lab/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY vps/lab/app/ ./app/
COPY vps/lab/web/ ./web/
COPY vps/lab/races/ ./races/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
