# One image for the API (and, later, the background worker) - Phase 2, Q7.3.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Set to "true" for local development / CI images (adds pytest, httpx, ruff).
ARG INSTALL_DEV=false

COPY pyproject.toml ./
COPY app ./app
# The base image's pip has known advisories (pip-audit, 2026-10-16); it isn't used at runtime,
# but upgrade it anyway.
RUN pip install --upgrade "pip>=26.2"
# Production images drop pip once dependencies are in: nothing uses it at runtime, and its
# vendored-package SBOM (pip/_vendor/bom.cdx.json: urllib3, msgpack, setuptools) fails the
# Trivy scan. Dev images keep it for pip-audit and ad-hoc installs.
RUN if [ "$INSTALL_DEV" = "true" ]; then \
        pip install ".[dev]"; \
    else \
        pip install . && pip uninstall --yes pip; \
    fi

COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY tests ./tests

RUN useradd --create-home --uid 1000 appuser && chmod +x scripts/*.sh
USER appuser

EXPOSE 8000
CMD ["./scripts/start-api.sh"]
