ARG FORCE_REBUILD=1

FROM ghcr.io/astral-sh/uv:0.5-python3.13-alpine

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/usr/src/app/.venv/bin:$PATH" \
    PYTHONPATH="/usr/src/app"

WORKDIR /usr/src/app

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --group bot --no-group admin --no-group dev \
    && pybabel compile -d ./bot/locales \
    && adduser -D appuser \
    && chown -R appuser:appuser .

USER appuser

CMD ["python", "-m", "bot"]
