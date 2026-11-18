# Fly.io image for the API (ARCHITECTURE §Hosting). Pins Python 3.12 to match .python-version.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code-only changes reuse this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# Run as an unprivileged user (M6 gate finding 8); the app writes nothing to disk.
RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8080
# Fly injects PORT; 8080 matches internal_port in fly.toml.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
