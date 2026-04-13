FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential openssl git && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Optional OCI label only (not read by the app). Example:
#   CHILI_GIT_COMMIT=$(git rev-parse HEAD) docker compose build chili
ARG CHILI_GIT_COMMIT=
LABEL org.opencontainers.image.revision="${CHILI_GIT_COMMIT}"

RUN mkdir -p /app/data /app/docker-certs && \
    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout /app/docker-certs/server.key \
      -out /app/docker-certs/server.pem \
      -days 3650 \
      -subj "/CN=localhost" \
      -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:0:0:0:0:0:0:0:1" && \
    chmod 644 /app/docker-certs/server.pem && chmod 600 /app/docker-certs/server.key

EXPOSE 8000

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV OLLAMA_HOST=http://ollama:11434
# Set to 0 to serve plain HTTP inside the container (not recommended).
ENV CHILI_TLS=1

# Windows checkouts may use CRLF; strip \r so the shebang works in Linux.
RUN sed -i 's/\r$//' /app/scripts/docker-entrypoint-chili.sh && chmod +x /app/scripts/docker-entrypoint-chili.sh

ENTRYPOINT ["/app/scripts/docker-entrypoint-chili.sh"]
