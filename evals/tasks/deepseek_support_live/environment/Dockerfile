FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl openssl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/local/share/ca-certificates \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout /etc/ssl/private/api.deepseek.com.key \
        -out /usr/local/share/ca-certificates/api.deepseek.com.crt \
        -subj "/CN=api.deepseek.com" \
        -addext "subjectAltName=DNS:api.deepseek.com" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign,cRLSign" \
        -addext "extendedKeyUsage=serverAuth" \
    && update-ca-certificates \
    && pip install --no-cache-dir openai==3.29.0 'litellm[proxy]==1.107.0.dev3'
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NO_PROXY=api.deepseek.com,127.0.0.1,localhost \
    no_proxy=api.deepseek.com,127.0.0.1,localhost \
    LITELLM_TELEMETRY=False \
    LITELLM_LOCAL_MODEL_COST_MAP=True
WORKDIR /workspace
COPY workspace/ /workspace/
COPY eval/ /eval/
RUN mkdir -p /logs/agent /logs/verifier
