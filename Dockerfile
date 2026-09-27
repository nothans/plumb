# Plumb: one process, one SQLite file, no network needed at runtime.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLUMB_DATA_DIR=/data

WORKDIR /srv/plumb
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY fixtures.json ./
COPY tools ./tools

RUN useradd --system --uid 10001 plumb && mkdir -p /data && chown plumb /data
USER plumb
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/projects', timeout=2)"

# Bootstrap (schema, signing key, demo seed when PLUMB_DEMO=1) then serve.
# X-Forwarded-For is trusted only from the addresses in FORWARDED_ALLOW_IPS
# (uvicorn's setting; default 127.0.0.1). Behind a reverse proxy, set it to the
# proxy's address, never to "*": that would let any client pick its own IP and
# walk around every per-address limit.
CMD ["sh", "-c", "python -m app.cli bootstrap && exec uvicorn app.main:get_app --factory --host 0.0.0.0 --port 8080"]
