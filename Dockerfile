# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM node:24-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json frontend/
RUN npm --prefix frontend ci --ignore-scripts
COPY frontend/ frontend/
ARG VITE_FIREBASE_API_KEY
ARG VITE_FIREBASE_AUTH_DOMAIN
ARG VITE_FIREBASE_PROJECT_ID
RUN npm --prefix frontend run build

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS app
COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 HOME=/tmp \
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
