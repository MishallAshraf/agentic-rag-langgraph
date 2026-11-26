#!/usr/bin/env bash
# Start the fake model, N API copies and the build worker with the load-test settings, in the background.
#
#   bash loadtest/start_copies.sh 2        # copies on ports 8001 and 8002, plus one worker
#   SCALE=1 bash loadtest/start_copies.sh 3   # also nginx on 8000 and the read replica (Step 9)
#   bash loadtest/start_copies.sh stop     # stop them all
#
# Each copy is a separate process with its own memory, sharing one Postgres (the
# loadtest schema) and, from Phase 1 Step 7, one Redis: the setup Step 9 puts
# behind a load balancer. From Step 8, bot builds run in a separate worker process
# (engine/worker.py; WORKERS=0 to run none). Logs and process ids go to data/loadtest/.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/loadtest

if [ "${1:-}" = "stop" ]; then
  pkill -f "loadtest/mock_llm.py" || true
  pkill -f "python -m engine.api" || true
  pkill -f "python -m engine.worker" || true
  sleep 3  # a process stuck in start-up ignores the polite signal, so then force it
  pkill -9 -f "python -m engine.api" || true
  pkill -9 -f "python -m engine.worker" || true
  docker compose --profile scale stop nginx db-replica >/dev/null 2>&1 || true
  exit 0
fi

export DATABASE_URL="host=localhost user=lampus password=lampus dbname=lampus options='-c search_path=loadtest,public'"
export LLM_BASE_URL="http://127.0.0.1:8900/v1"
export API_RATE_LIMIT="${API_RATE_LIMIT:-1000}"
export QUOTA_TURNS_PER_DAY=100000 QUOTA_TOKENS_PER_DAY=100000000
export LANGFUSE_PUBLIC_KEY= LANGFUSE_SECRET_KEY=
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-placeholder-not-a-key}"
export REDIS_PREFIX="loadtest:"
WORKERS="${WORKERS:-1}"
[ -f engine/worker.py ] || WORKERS=0   # before Step 8 builds ran inside the API
export API_RUN_WORKER=$([ "$WORKERS" -gt 0 ] && echo 0 || echo 1)

pgrep -f "loadtest/mock_llm.py" >/dev/null || nohup python loadtest/mock_llm.py > data/loadtest/mock_llm.log 2>&1 &
if [ "${SCALE:-0}" = "1" ]; then
  # A fresh replica (copied from the primary now), and nginx in front of the copies.
  docker compose --profile scale up -d --wait --force-recreate --renew-anon-volumes db-replica nginx 2>&1 | grep -v "^ *Container" || true
  READ_URL="host=localhost port=5433 user=lampus password=lampus dbname=lampus options='-c search_path=loadtest,public'"
fi
python loadtest/run.py setup --users 50 > /dev/null
# The café bot's chunks, in the loadtest schema (quick after the first time: unchanged files are skipped)
python -m engine.ingest bots/customer_handbook.yaml 2>&1 | grep Ingested
for i in $(seq 1 "${1:-2}"); do
  # Only the API copies search the replica; workers read the chunks they just wrote.
  DATABASE_READ_URL="${READ_URL:-}" API_PORT=$((8000 + i)) nohup python -m engine.api > "data/loadtest/api_$i.log" 2>&1 &
  echo $! > "data/loadtest/api_$i.pid"
done
for i in $(seq 1 "$WORKERS"); do
  nohup python -m engine.worker > "data/loadtest/worker_$i.log" 2>&1 &
  echo $! > "data/loadtest/worker_$i.pid"
done
for i in $(seq 1 "${1:-2}"); do  # wait until each copy answers
  until curl -s -o /dev/null "http://127.0.0.1:$((8000 + i))/bots"; do sleep 1; done
done
echo "fake model on 8900, API copies on ports 8001-$((8000 + ${1:-2})), $WORKERS worker(s)$([ -n "${READ_URL:-}" ] && echo ", nginx on 8000, read replica on 5433")"
