FROM ghcr.io/astral-sh/uv:0.11.17 AS uv
FROM public.ecr.aws/docker/library/python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS python-base
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --link-mode=copy

FROM python-base AS contracts
COPY app ./app
COPY sandbox ./sandbox
COPY agent ./agent
COPY scripts/export_frontend_schema.py ./scripts/export_frontend_schema.py
RUN .venv/bin/python scripts/export_frontend_schema.py

FROM public.ecr.aws/docker/library/node:22-slim AS frontend
WORKDIR /frontend
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend ./frontend
COPY --from=contracts /app/frontend/api/openapi.json ./frontend/api/openapi.json
COPY scripts/generate_routes.mjs ./scripts/generate_routes.mjs
COPY vite.config.ts vite.app.config.ts tsconfig.json openapi-ts.config.ts ./
RUN npm run api:client && npm run routes:generate && npm exec tsc -- --noEmit && npm run build:assets

FROM python-base
COPY app ./app
COPY --from=frontend /frontend/app/static/generated/ui ./app/static/generated/ui
COPY --from=frontend /frontend/app/static/spa ./app/static/spa
COPY sandbox ./sandbox
COPY agent ./agent
COPY render_start.py build_workspace_image.py docker_start.py docker_healthcheck.py ./
RUN useradd --uid 10001 --create-home workspace && mkdir /data && chown workspace:workspace /data
# The entrypoint adopts the mounted data tree, then drops to workspace before
# importing application code. A build-time chown cannot fix a mounted disk.
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD /app/.venv/bin/python /app/docker_healthcheck.py
ENTRYPOINT ["/app/.venv/bin/python", "/app/docker_start.py"]
