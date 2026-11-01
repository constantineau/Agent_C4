# n2kout — the N2K route broadcaster (host network for SocketCAN; same image bench + boat).
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1

# iproute2: `ip -details link` is how /health reads the CAN controller state (bus-off etc.)
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

COPY pi/n2kout/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pi/n2kout/n2k.py pi/n2kout/navcalc.py pi/n2kout/n2kout_app.py ./

# Host network: the engine on the same Pi is the only caller, so listen on localhost only.
EXPOSE 8210
CMD ["uvicorn", "n2kout_app:app", "--host", "127.0.0.1", "--port", "8210"]
