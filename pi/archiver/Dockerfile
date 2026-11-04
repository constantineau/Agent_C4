# Built from repo root (context: .) to match the uplink image layout.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

# copyoff.py ships archive segments off the boat with rsync over ssh (#181)
RUN apt-get update && apt-get install -y --no-install-recommends rsync openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY pi/archiver/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY pi/archiver/archiver.py ./archiver.py
COPY pi/archiver/backfill.py ./backfill.py
COPY pi/archiver/copyoff.py ./copyoff.py

CMD ["python", "archiver.py"]
