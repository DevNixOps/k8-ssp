#!/usr/bin/env bash
# Starts backend (:8000) and frontend (:5173). Usage: ./run.sh [mock|real]   (default: real)
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-real}"
[ "$MODE" = "mock" ] && export SSP_MOCK_MODE=true || export SSP_MOCK_MODE=false
export SSP_JWT_SECRET="${SSP_JWT_SECRET:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')}"

if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q -r backend/requirements.txt
fi
[ -d frontend/node_modules ] || (cd frontend && npm install)

(cd backend && PYTHONPATH=. .venv/bin/uvicorn app.main:app --port 8000) &
BACKEND=$!
trap 'kill $BACKEND 2>/dev/null' EXIT

for _ in $(seq 1 30); do
  curl -sf localhost:8000/api/health >/dev/null && break
  sleep 1
done
curl -sf localhost:8000/api/health >/dev/null || { echo "Backend failed to start"; exit 1; }

echo "Backend ready (mode=$MODE). Open http://localhost:5173  (login: admin/admin)"
cd frontend && npm run dev
