#!/usr/bin/env bash
# Run AURA's local background workers together; Ctrl+C stops all of them.
#   jobs          Shadow runs, async/graph runs (incl. approvals), prompt optimization
#   outbox-relay  pushes config changes to every server's cache
#   credits       monthly consumer credit grants + margin alert
# (telemetry needs NATS + ClickHouse, which are not part of the local setup.)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .local
trap 'kill 0' EXIT INT TERM
for w in jobs outbox-relay credits; do
  uv run aura-workers "$w" >".local/worker-$w.log" 2>&1 &
  echo "started worker '$w' (log: .local/worker-$w.log)"
done
wait
