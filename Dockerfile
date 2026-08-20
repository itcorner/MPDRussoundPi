FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ser2net \
    && rm -rf /var/lib/apt/lists/*

COPY web/requirements.txt /app/web/requirements.txt
RUN pip install --no-cache-dir -r /app/web/requirements.txt

COPY web/ /app/web/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# /config and /data are mount points; both must stay writable for atomic JSON writes.
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && useradd --system --uid 10001 russound \
    && mkdir -p /config /data \
    && chown -R russound:russound /app /config /data \
    && chown russound:russound /run/lock

USER russound

ENV RUSSOUND_WEB_HOST=0.0.0.0 \
    RUSSOUND_WEB_PORT=8000 \
    RUSSOUND_CONFIG=/config/russound_config.json \
    RUSSOUND_STATE=/data/russound_state.json \
    RUSSOUND_BACKEND_HOST=127.0.0.1 \
    RUSSOUND_BACKEND_PORT=6666 \
    RUSSOUND_SER2NET_ENABLED=true \
    RUSSOUND_SER2NET_BIND=localhost \
    RUSSOUND_SER2NET_PORT=6666 \
    RUSSOUND_SERIAL_DEVICE=/dev/ttyUSB0 \
    RUSSOUND_SERIAL_OPTIONS=19200n81

EXPOSE 8000
VOLUME ["/config", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('RUSSOUND_WEB_PORT', '8000') + '/', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "web.russound_server"]
