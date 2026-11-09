FROM ghcr.io/astral-sh/uv:0.11.32 AS uv
FROM ghcr.io/xtls/xray-core:26.10.31 AS xray
FROM python:3.14-slim

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
COPY --from=xray /usr/local/share/xray/ /usr/local/share/xray/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    XRAY_LOCATION_ASSET=/usr/local/share/xray

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --group deploy --no-install-project --no-cache

COPY alembic.ini database.py main.py models.py web.py ./
COPY migrations/ ./migrations/
COPY reader_web/ ./reader_web/
COPY sites/ ./sites/
COPY static/ ./static/
COPY templates/ ./templates/
COPY utils/ ./utils/

RUN chmod 0644 pyproject.toml uv.lock README.md alembic.ini database.py main.py models.py web.py \
    && chmod -R a+rX,go-w migrations reader_web sites static templates utils \
    && groupadd --gid 568 reader \
    && useradd --uid 568 --gid reader --create-home reader \
    && install -d -o reader -g reader /app/data

USER reader
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "web:create_app()"]
