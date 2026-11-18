# Multi-stage image for the invoice bot. Works unchanged with Docker or Podman:
# every image reference is fully qualified and only classic Dockerfile syntax is used.

FROM docker.io/library/python:3.12.16-slim@sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev


FROM docker.io/library/python:3.12.16-slim@sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177

WORKDIR /app

RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --create-home --shell /usr/sbin/nologin bot

# OCR engine (D81): the image helper runs `tesseract` as a subprocess with the Spanish model.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Browser for portal fetchers (D82): Chromium's headless shell only, plus its system
# libraries, in a shared path the bot user can read. The browser helper starts it per fetch.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN /app/.venv/bin/playwright install --only-shell --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/ms-playwright

# Migration and config tooling available at runtime: `alembic upgrade head` and
# `python scripts/check_config.py <path>` run inside the image. Owned by root,
# readable by the bot user.
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY config/example.yaml ./config/example.yaml
COPY config/parsing_rules.yaml ./config/parsing_rules.yaml
RUN chmod -R a+rX /app/alembic.ini /app/migrations /app/scripts /app/config

# Media and work files. A new named volume copies this owner, so the bot can write.
RUN mkdir -p /data && chown bot:bot /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HELPER_SANDBOX=1

USER bot

EXPOSE 8080

CMD ["uvicorn", "invoicebot.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
