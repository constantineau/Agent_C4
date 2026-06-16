# Built from repo root (context: .) so it can include shared/.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

COPY pi/uplink/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY pi/uplink/uplink.py ./uplink.py

CMD ["python", "uplink.py"]
