# =============================================================================
# Multi-stage Dockerfile for VM Placement Solver sidecar
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Build — install dependencies into a virtual environment
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

WORKDIR /build

# Copy project metadata first for better layer caching
COPY pyproject.toml .

# Create venv and install dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source (needed by pip install . since pyproject.toml
# declares packages = ["app"])
COPY app/ app/

RUN pip install --no-cache-dir .

# ---------------------------------------------------------------------------
# Stage 2: Runtime — minimal image with only what's needed
# ---------------------------------------------------------------------------
FROM python:3.13-slim

RUN groupadd -r solver && useradd -r -g solver -d /app -s /sbin/nologin solver

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/app /app/app
COPY pyproject.toml /app/

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH"

USER solver

EXPOSE 50051

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:50051/healthz')"

ENTRYPOINT ["python", "-m", "app.server", "--port", "50051"]
