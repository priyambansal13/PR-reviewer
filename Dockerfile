# One image for every service; docker-compose picks the command. Dependencies are a separate layer,
# so code changes rebuild in seconds.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY common common
COPY services services
COPY migrations migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 1001 app
USER app
