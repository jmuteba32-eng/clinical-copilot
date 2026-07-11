# Clinical Co-Pilot agent service.
#
# Build context is expected to be this `agent/` directory (e.g. Railway's
# "root directory" set to `agent`, or `docker build -f agent/Dockerfile agent`).

FROM python:3.11-slim

WORKDIR /app

# Dependencies in their own layer so a source-only change doesn't bust the
# pip install cache on every rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copied into ./agent/ (not flattened into /app) so `agent.main`,
# `agent.tools`, etc. keep resolving the way they do in local development,
# where everything is run from the repo root with `agent` as the package.
# See .dockerignore for what's deliberately excluded -- most importantly
# `.env` and `venv/`, which must never end up baked into an image layer.
COPY . ./agent/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Documents the fallback port main.py binds to when the platform doesn't
# inject PORT; Railway (and most PaaS) assign PORT dynamically at runtime,
# which EXPOSE cannot express -- it's metadata, not a binding restriction.
EXPOSE 8000

# Least privilege: this process only ever makes outbound HTTPS calls to
# OpenEMR and Anthropic, so it has no legitimate reason to run as root
# during request handling. We still start as root via the entrypoint so
# Railway's mounted /data volume (root-owned at attach time) can be
# chowned to appuser before uvicorn drops privileges.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER root
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Shell form so ${PORT:-8000} expands at container start, when the platform's
# actual PORT value is known. --no-access-log avoids duplicating the
# structured "http_request" summary line main.py's correlation middleware
# already emits (with correlation_id and latency_ms, which uvicorn's own
# access log lacks).
CMD ["sh", "-c", "uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
