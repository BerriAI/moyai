FROM ghcr.io/astral-sh/uv:0.11.17 AS uv
FROM node:26-bookworm-slim@sha256:5df79989ed685e09f0a98019db4cebfb22fb9df1a53c0161664a011f7ff6b0f1 AS native-ui
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY frontend ./frontend
COPY native/web ./native/web
RUN npm run build:native

FROM rust:1.88-slim-bookworm@sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89 AS rust
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS python-base
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# Compile AgentChat's pinned native SDK in the builder, keeping Rust out of runtime.
FROM python-base AS dependencies
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup PATH="/usr/local/cargo/bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
COPY native/pyproject.toml ./native/
RUN uv sync --frozen --no-dev --no-install-workspace

FROM python-base
COPY pyproject.toml uv.lock ./
COPY native/pyproject.toml ./native/
COPY --from=dependencies /app/.venv /app/.venv
COPY app ./app
COPY native/src ./native/src
COPY --from=native-ui /build/native/src/moyai/static ./native/src/moyai/static
COPY native/skills ./native/skills
COPY native/instructions.md native/desktop.py native/browser_guest.py native/browser_driver.py native/ocu_guest.py native/hatch_build.py ./native/
RUN uv sync --frozen --no-dev --no-editable
# Runtime uses the uv environment. Remove system pip, its bootstrap wheel, and
# build caches so scanners do not report unused vendored dependencies.
RUN /usr/local/bin/python -m pip uninstall --yes pip \
    && /usr/local/bin/python -c "import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)" \
    && uv cache clean
COPY render_start.py build_workspace_image.py docker_start.py docker_healthcheck.py ./
ENV PYTHONPATH=/app
RUN /app/.venv/bin/python -c "from moyai.product_deployment import check_runtime_dependencies; check_runtime_dependencies()"
RUN useradd --uid 10001 --create-home workspace && mkdir /data && chown workspace:workspace /data
ARG MOYAI_BUILD_SHA=""
LABEL org.opencontainers.image.source="https://github.com/BerriAI/moyai"
LABEL org.opencontainers.image.revision="${MOYAI_BUILD_SHA}"
RUN printf '%s\n' "$MOYAI_BUILD_SHA" > /app/build-revision
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 MOYAI_CORE=pydantic PATH="/app/.venv/bin:${PATH}"
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD /app/.venv/bin/python /app/docker_healthcheck.py
ENTRYPOINT ["/app/.venv/bin/python", "/app/docker_start.py"]
