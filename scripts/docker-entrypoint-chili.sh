#!/bin/sh
set -e
# Share Robinhood session tokens across containers via /app/data/.tokens
mkdir -p /app/data/.tokens
if [ ! -e "$HOME/.tokens" ]; then
  ln -s /app/data/.tokens "$HOME/.tokens"
fi
# docker-compose `command:` (e.g. brain-worker) passes args here — run them instead of uvicorn.
if [ "$#" -gt 0 ]; then
  echo "[docker-entrypoint-chili] CHILI_SCHEDULER_ROLE=${CHILI_SCHEDULER_ROLE:-unset} (non-web command)"
  exec "$@"
fi
# Log mode for docker logs (runtime evidence: HTTPS vs HTTP).
echo "[docker-entrypoint-chili] CHILI_TLS=${CHILI_TLS:-unset} (0=plain HTTP, else=HTTPS)"
echo "[docker-entrypoint-chili] CHILI_SCHEDULER_ROLE=${CHILI_SCHEDULER_ROLE:-unset} (web: none = no APScheduler in Uvicorn)"
# CHILI_TLS=0 disables TLS (plain HTTP) for debugging only.
if [ "${CHILI_TLS:-1}" != "0" ]; then
  exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --ssl-certfile /app/docker-certs/server.pem \
    --ssl-keyfile /app/docker-certs/server.key
else
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000
fi
