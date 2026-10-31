# syntax=docker/dockerfile:1
#
# Sentinel 2.0 API image.
#
# Scope: the HTTP API entry point only (`python -m api`). The outbox delivery
# worker (`python -m api.event_worker`) and schema migration CLI are separate
# run modes and are intentionally not started here.
#
# The image contains only the packages the API needs at runtime:
#   api/      HTTP layer and entry point
#   agents/   investigation pipeline used by the routes
#   core/     domain, events, LLM and persistence (incl. persistence/sql/)
#   schemas/  JSON schemas read from disk at runtime by the agents
# Tests, incident datasets, evaluation fixtures and local secrets are excluded
# via .dockerignore and are deliberately never copied in.

FROM python:3.12-slim

# Predictable working directory. Filesystem persistence defaults to
# `.sentinel_persistence` relative to the working directory, so this path is
# also the runtime data root created below.
WORKDIR /app

# Runtime dependencies only (no test/dev tooling). Pinned via
# requirements-runtime.txt for reproducible builds. Copied first so this layer
# is cached independently of code changes; --no-cache-dir keeps pip's
# download/build cache out of the final image.
COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

# Application code required to start the API (see file header).
COPY api/ ./api/
COPY agents/ ./agents/
COPY core/ ./core/
COPY schemas/ ./schemas/

# Dedicated unprivileged runtime user. Only the runtime data directories need to
# be writable; the copied application files stay root-owned but world-readable.
#
# /app/incidents is the incident-bundle root the API resolves by default
# (`incidents` relative to WORKDIR). It is created here and owned by the runtime
# user so that a named/bind volume mounted at this path on a fresh volume
# inherits that ownership — otherwise Docker creates the mount point as
# root:root and the non-root API cannot write ingested bundles into it.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sentinel \
    && mkdir -p /app/.sentinel_persistence /app/incidents \
    && chown -R sentinel:sentinel /app/.sentinel_persistence /app/incidents

USER sentinel

# Documented/default API port (see api/__main__.py).
EXPOSE 8000

# Readiness probe against the real dependency check (GET /ready). Implemented
# with the Python standard library only — no curl/wget is added to the image.
# A 503/connection failure raises and exits non-zero; sys.tracebacklimit keeps
# the container log free of stack traces.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.tracebacklimit = 0; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4).status == 200 else 1)"

# Start the existing entry point, bound to all interfaces for container use.
# Exec form keeps python as PID 1 so uvicorn receives SIGTERM directly and can
# shut down gracefully.
CMD ["python", "-m", "api", "--host", "0.0.0.0", "--port", "8000"]
