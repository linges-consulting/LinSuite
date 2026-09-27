FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH"

# Not root. `/var/lib/linsuite` is created here and owned by the user, so the named volume
# compose mounts over it inherits that ownership on first use — the 0600 setup token has to
# be writable, and nothing else does.
RUN useradd --system --uid 10001 --create-home linsuite \
    && install -d -o linsuite -g linsuite /var/lib/linsuite
USER linsuite

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
