FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the code.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backy/ backy/
RUN uv sync --frozen --no-dev

# No database client here on purpose: pg_dump runs inside the database container, so this
# image never has to match the server's Postgres major version.
#
# ponytail: runs as root. The container needs /var/run/docker.sock, which is already
# root-equivalent on the host, so a non-root user would buy nothing here.
ENTRYPOINT ["uv", "run", "--no-sync", "python", "-m", "backy"]
