FROM ghcr.io/astral-sh/uv:0.12-python3.14-alpine

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/usr/src/app/.venv/bin:$PATH"

WORKDIR /usr/src/app

# `age` is how the backup task encrypts a dump before it leaves the machine
# (`age -r <X25519 public key>`), and it is a system binary, not a Python dependency.
# The deployment holds only the public half of the keypair, so nothing in this image can
# decrypt what it produced. Alpine community ships it; pinned to no version on purpose —
# the format is stable and `age` is a security-relevant binary worth getting patches for.
RUN apk add --no-cache age

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --group bot --no-group dev \
    && pybabel compile -d ./bot/locales \
    && adduser -D appuser \
    && chown -R appuser:appuser .

USER appuser

# The API entrypoint, not `python -m bot` (long polling) — polling is a development
# path and serves neither /metrics, /health/* nor the Mini App. The container port is
# fixed at 8080; compose maps ${WEBHOOK_PORT} to it, so the host port stays configurable
# while Prometheus can scrape a stable `api:8080`.
CMD ["uvicorn", "bot.entrypoints.api:app", "--host", "0.0.0.0", "--port", "8080"]
