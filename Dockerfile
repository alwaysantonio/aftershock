FROM python:3.14-slim

# uv binary from the official image (pin a version for reproducible builds)
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    STATIC_ROOT=/opt/static \
    PATH="/app/.venv/bin:$PATH"

# 1 = include the dev group (pytest etc.) so tests run in the container
ARG INSTALL_DEV=1

WORKDIR /app

# Dependencies first, in their own layer: only rebuilt when the lock file changes.
# --locked fails the build if uv.lock is out of date with pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    if [ "$INSTALL_DEV" = "1" ]; then uv sync --locked --no-install-project; \
    else uv sync --locked --no-install-project --no-dev; fi

COPY . .

# Collect static files (admin + browsable API CSS/JS) into the image at build
# time. Settings only need their defaults here: no database is touched.
RUN python manage.py collectstatic --noinput

# Run as a non-root user
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8000