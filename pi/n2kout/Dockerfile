# n2kout — the N2K route broadcaster (host network for SocketCAN; same image bench + boat).
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1

COPY pi/n2kout/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pi/n2kout/n2k.py pi/n2kout/n2kout_app.py ./

EXPOSE 8210
CMD ["uvicorn", "n2kout_app:app", "--host", "0.0.0.0", "--port", "8210"]
