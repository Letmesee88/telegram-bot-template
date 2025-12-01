FROM python:3.13-alpine

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/usr/src/app/.venv/bin:/root/.local/bin:/root/.cargo/bin:$PATH"

WORKDIR /usr/src/app

COPY . .

# Install minimal build deps and curl, then install uv via the official script
RUN apk add --no-cache curl ca-certificates build-base libffi-dev openssl-dev \
    && update-ca-certificates \
    && curl -fsSL https://astral.sh/uv/install.sh | sh -s -- -y

# Use uv to create and populate a local .venv with only the admin dependency group
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --group admin --no-group bot --no-group dev \
    && adduser -D appuser \
    && chown -R appuser:appuser .

USER appuser

CMD ["gunicorn", "-c", "admin/gunicorn_conf.py"]
