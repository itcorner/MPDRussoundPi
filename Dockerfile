# UNTESTED: this image has not been built or run against real Russound hardware yet.
# Web application only. The serial gateway lives in Dockerfile.ser2net and the
# hardware-free backend in Dockerfile.dummy.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY web/requirements.txt /app/web/requirements.txt
RUN pip install --no-cache-dir -r /app/web/requirements.txt

COPY web/ /app/web/
COPY mpdrussoundpi/russound/lib/sourceurl.py mpdrussoundpi/russound/lib/tunein.py /app/mpdrussoundpi/russound/lib/

# /config and /data are mount points; both must stay writable for atomic JSON writes.
RUN useradd --system --uid 10001 russound \
    && mkdir -p /config /data /logs \
    && chown -R russound:russound /app /config /data /logs

USER russound

ENV RUSSOUND_WEB_HOST=0.0.0.0 \
    RUSSOUND_WEB_PORT=8000 \
    RUSSOUND_CONFIG=/config/russound_config.json \
    RUSSOUND_STATE=/data/russound_state.json \
    RUSSOUND_LOG_FILE=/logs/russound-web.log

EXPOSE 8000
VOLUME ["/config", "/data", "/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('RUSSOUND_WEB_PORT', '8000') + '/', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "web.russound_server"]
