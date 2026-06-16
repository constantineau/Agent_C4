# Built from repo root (context: .) to match the uplink image layout.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

COPY pi/archiver/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pi/archiver/archiver.py ./archiver.py
COPY pi/archiver/backfill.py ./backfill.py

CMD ["python", "archiver.py"]
