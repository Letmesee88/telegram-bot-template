FROM python:3.13-alpine

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/usr/src/app/.venv/bin:/root/.local/bin:/root/.cargo/bin:$PATH" \
    PYTHONPATH="/usr/src/app"

WORKDIR /usr/src/app

COPY . .

# Install minimal build deps and curl, then install uv via the official script
RUN apk add --no-cache curl ca-certificates build-base libffi-dev openssl-dev \
    && update-ca-certificates \
    && curl -fsSL https://astral.sh/uv/install.sh | sh -s -- -y

# Create a local .venv and install only the 'bot' dependency group
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --group bot --no-group admin --no-group dev \
    && pybabel compile -d ./bot/locales \
    && adduser -D appuser \
    && chown -R appuser:appuser .

USER appuser

CMD ["python", "-m", "bot"]
