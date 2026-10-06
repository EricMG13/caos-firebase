# syntax=docker/dockerfile:1
FROM node:24-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json frontend/
RUN npm --prefix frontend ci --ignore-scripts
COPY frontend/ frontend/
ARG VITE_FIREBASE_API_KEY
ARG VITE_FIREBASE_AUTH_DOMAIN
ARG VITE_FIREBASE_PROJECT_ID
RUN npm --prefix frontend run build

FROM python:3.13-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev
COPY caos/ caos/
COPY icm/ icm/
COPY vendor/ vendor/
COPY --from=frontend /src/frontend/dist frontend/dist
RUN useradd --uid 10001 --no-create-home caos
USER caos
ENV CAOS_SITE_ROOT=frontend/dist CAOS_BIND_HOST=0.0.0.0 CAOS_WORKER_IN_PROCESS=1
EXPOSE 8000
CMD ["/app/.venv/bin/python", "-m", "caos.serve"]
