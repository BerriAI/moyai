FROM ghcr.io/astral-sh/uv:0.11.17 AS uv
FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY inference ./inference
COPY sandbox ./sandbox
RUN useradd --uid 10001 --create-home workspace && mkdir /data && chown workspace:workspace /data
USER workspace
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD /app/.venv/bin/python -c "import os,urllib.request,urllib.parse; host=urllib.parse.urlparse(os.environ['PUBLIC_URL']).netloc; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8787/health', headers={'Host':host}), timeout=3)"
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1"]
