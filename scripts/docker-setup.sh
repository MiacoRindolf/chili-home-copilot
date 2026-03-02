#!/usr/bin/env bash
set -e

echo "=== CHILI Docker Setup ==="
echo ""

echo "[1/4] Starting containers..."
docker compose up -d ollama
echo "  Waiting for Ollama to be healthy..."
docker compose exec ollama bash -c 'until curl -sf http://localhost:11434/api/tags > /dev/null; do sleep 2; done'

echo "[2/4] Pulling LLM models (this may take a few minutes on first run)..."
docker compose exec ollama ollama pull llama3
docker compose exec ollama ollama pull nomic-embed-text

echo "[3/4] Starting CHILI app..."
docker compose up -d chili

echo "[4/4] Ingesting documents for RAG..."
docker compose exec chili python -m app.ingest

echo ""
echo "=== Ready! ==="
echo "  Chat:  http://localhost:8000/chat"
echo "  Admin: http://localhost:8000/admin"
echo "  Health: http://localhost:8000/health"
