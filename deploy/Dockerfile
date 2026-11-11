# Multi-stage image for the invoice bot. Works unchanged with Docker or Podman:
# every image reference is fully qualified and only classic Dockerfile syntax is used.

FROM docker.io/library/python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev


FROM docker.io/library/python:3.12-slim

WORKDIR /app

RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --create-home --shell /usr/sbin/nologin bot

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER bot

EXPOSE 8080

CMD ["uvicorn", "invoicebot.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
