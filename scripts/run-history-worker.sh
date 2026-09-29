#!/usr/bin/env bash
set -euo pipefail

cd /app

# Each bounded run has 10 windows; the outer loop resumes from checkpoints.
# Any provider error stops the container instead of retrying indefinitely.
while true; do
  BACKFILL_BATCHES=100 bash scripts/backfill-history-local.sh
  remaining="$(python -c \
    'import json; print(int(json.load(open("reports/quant/backfill/last-batch.json", encoding="utf-8"))["remaining_window_count"]))')"
  if (( remaining == 0 )); then
    echo "历史日线计划窗口已回补完毕。"
    exit 0
  fi
done
