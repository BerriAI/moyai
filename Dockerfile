FROM ghcr.io/astral-sh/uv:0.11.17 AS uv
FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY sandbox ./sandbox
COPY render_start.py build_workspace_image.py docker_start.py docker_healthcheck.py ./
RUN useradd --uid 10001 --create-home workspace && mkdir /data && chown workspace:workspace /data
# The entrypoint adopts the mounted data tree, then drops to workspace before
# importing application code. A build-time chown cannot fix a mounted disk.
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD /app/.venv/bin/python /app/docker_healthcheck.py
ENTRYPOINT ["/app/.venv/bin/python", "/app/docker_start.py"]
