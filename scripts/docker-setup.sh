#!/usr/bin/env bash
set -e

echo "=== CHILI Docker Setup ==="
echo ""

echo "[1/4] Starting PostgreSQL and Ollama..."
docker compose up -d postgres ollama

echo "  Waiting for PostgreSQL to be healthy..."
until docker compose exec -T postgres pg_isready -U chili -d chili > /dev/null 2>&1; do
  sleep 1
done
echo "  PostgreSQL is ready."

echo "  Waiting for Ollama to be healthy..."
docker compose exec ollama bash -c 'until curl -sf http://localhost:11434/api/tags > /dev/null; do sleep 2; done'

echo "[2/4] Pulling LLM models (this may take a few minutes on first run)..."
docker compose exec ollama ollama pull llama3
docker compose exec ollama ollama pull nomic-embed-text

echo "[3/5] Starting CHILI app (DATABASE_URL -> postgres service)..."
docker compose up -d chili

echo "[4/4] Ingesting documents for RAG..."
docker compose exec chili python -m app.ingest

echo ""
echo "=== Ready! ==="
echo "  Chat:   https://localhost:8000/chat  (accept self-signed cert warning in browser)"
echo "  Admin:  http://localhost:8000/admin"
echo "  Health: http://localhost:8000/health"
echo "  Postgres (from host): postgresql://chili:chili@localhost:5433/chili"
echo ""
echo "=== Stop / reset ==="
echo "  Stop:       docker compose down"
echo "  Wipe data:  docker compose down -v"
