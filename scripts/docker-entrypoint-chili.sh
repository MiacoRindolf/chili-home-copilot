#!/bin/sh
set -e
# glibc per-thread arenas (cap defaults to 8*ncores = 48 here) ratchet to the
# high-water mark of every thread's largest transient pandas/numpy working set
# and never return that memory to the OS — the scheduler-worker reached 9.9GB
# RSS in 13h (2026-07-31) with flat Python object counts. Two arenas measured
# peak -36% / idle-retention -93% on the multithreaded churn benchmark when
# combined with the mem_watcher malloc_trim tick. Override via env if needed.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
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
