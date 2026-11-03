FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# Dependencies first, so code changes don't rebuild them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
# Editable install (uv's default): `release-watch migrate` finds alembic/ next to src/.
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH
USER nobody
ENTRYPOINT ["release-watch"]
