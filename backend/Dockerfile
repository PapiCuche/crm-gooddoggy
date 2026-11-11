# syntax=docker/dockerfile:1
# Backend (ASGI). Versiones de ADR-012: Python 3.14.7, uv 0.12.19.
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.14.7-slim AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

FROM python:3.14.7-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY manage.py ./
COPY config ./config
COPY core ./core
COPY apps ./apps
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status == 200 else 1)"]
# --proxy-headers: confía en X-Forwarded-* solo de FORWARDED_ALLOW_IPS, obligatoria con la
# configuración por defecto de la imagen (production): ver «Dirección del cliente» en el README.
# --no-access-log: el log de acceso de uvicorn no pasa por el redactor (incluye query strings).
# --ws-max-size: tope de una trama WebSocket (el Inbox solo recibe `ping`, F6-07a).
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log", "--ws-max-size", "16384"]
