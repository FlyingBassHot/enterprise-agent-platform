FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts
COPY seed ./seed
COPY entrypoint.sh ./

RUN mkdir -p /data/files
ENV PATH="/app/.venv/bin:$PATH" \
    APP_DATABASE_URL="sqlite+aiosqlite:////data/platform.db" \
    APP_STORAGE_DIR="/data/files" \
    APP_AUTO_CREATE_TABLES="false"

EXPOSE 8000
CMD ["./entrypoint.sh"]
