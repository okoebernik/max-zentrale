FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyserial==3.5

WORKDIR /app
COPY max_logger.py max_send.py max_auth.py max_mqtt.py max_backup.py max_server.py ./
COPY web ./web

# Inside the container the UI must listen on all interfaces; the compose file
# publishes it on 127.0.0.1 of the host only.
ENV MAX_HOST=0.0.0.0 \
    MAX_DB=/data/max.db \
    MAX_PORT=tcp://192.168.1.50:2323 \
    TZ=Europe/Berlin
VOLUME /data
EXPOSE 8080
CMD ["python", "-u", "max_server.py", "--http", "8080"]
