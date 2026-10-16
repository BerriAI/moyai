FROM ghcr.io/astral-sh/uv:0.11.17 AS uv
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS native-ui
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY frontend ./frontend
COPY app/static ./app/static
COPY native/web ./native/web
RUN npm run build:native

FROM public.ecr.aws/docker/library/python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY sandbox ./sandbox
COPY agent ./agent
# Keep the transition locks separate: legacy OpenAI 2/Agents dependencies never
# enter the Pydantic runtime's environment. Copy only package inputs, not a host
# virtualenv, development data, or untracked frontend dependency trees.
COPY native/pyproject.toml native/uv.lock ./native/
RUN uv sync --project native --frozen --no-dev --extra product --no-install-project
COPY native/src ./native/src
COPY --from=native-ui /build/native/src/moyai/static ./native/src/moyai/static
COPY native/skills ./native/skills
COPY native/instructions.md native/desktop.py native/browser_guest.py native/hatch_build.py ./native/
RUN uv sync --project native --frozen --no-dev --extra product --no-editable
COPY render_start.py build_workspace_image.py docker_start.py docker_healthcheck.py ./
ENV PYTHONPATH=/app
RUN /app/native/.venv/bin/python -c "from moyai.product_deployment import check_runtime_dependencies; check_runtime_dependencies()"
RUN useradd --uid 10001 --create-home workspace && mkdir /data && chown workspace:workspace /data
ARG MOYAI_BUILD_SHA=""
LABEL org.opencontainers.image.source="https://github.com/BerriAI/moyai"
LABEL org.opencontainers.image.revision="${MOYAI_BUILD_SHA}"
RUN printf '%s\n' "$MOYAI_BUILD_SHA" > /app/build-revision
# The entrypoint adopts the mounted data tree, then drops to workspace before
# importing application code. A build-time chown cannot fix a mounted disk.
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD /app/.venv/bin/python /app/docker_healthcheck.py
ENTRYPOINT ["/app/.venv/bin/python", "/app/docker_start.py"]
