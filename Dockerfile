# The Apex backend image: the read-only API, and the one-off "seed" step that creates the
# database on first start (see compose.yaml).
FROM python:3.13.17-slim

# uv installs the exact package versions recorded in uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY pyproject.toml uv.lock LICENSE NOTICE ./
RUN uv sync --frozen --no-dev --no-install-project

# pip is never used at runtime (uv installed everything above). Removing it also removes the out-of-date
# copies of urllib3, msgpack and setuptools' pkg_resources that pip bundles (Trivy, 2026-10-27).
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3*

COPY apex ./apex
COPY scripts ./scripts
COPY schema ./schema
COPY config ./config
COPY data/seed ./data/seed

# Run as an ordinary user, not root.
RUN useradd --system --uid 10001 apex
USER apex

EXPOSE 8000
CMD ["python", "-m", "apex.serve"]
