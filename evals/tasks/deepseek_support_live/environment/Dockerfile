FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir openai==3.29.0
WORKDIR /workspace
COPY workspace/ /workspace/
COPY eval/ /eval/
RUN mkdir -p /logs/agent /logs/verifier
