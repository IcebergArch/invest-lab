#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

BACKFILL_PYTHON="${BACKFILL_PYTHON:-python3}"
BACKFILL_EXTRA_PYTHONPATH="${BACKFILL_EXTRA_PYTHONPATH:-}"
BACKFILL_SNAPSHOT="${BACKFILL_SNAPSHOT:-reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json}"
BACKFILL_DB="${BACKFILL_DB:-data/quant/historical-baostock-raw.sqlite3}"
BACKFILL_STATUS_OUT="${BACKFILL_STATUS_OUT:-reports/quant/backfill/historical-archive-status.json}"
BACKFILL_LAST_RESULT_OUT="${BACKFILL_LAST_RESULT_OUT:-reports/quant/backfill/last-batch.json}"
BACKFILL_BATCHES="${BACKFILL_BATCHES:-10}"
BACKFILL_INTERVAL_SECONDS="${BACKFILL_INTERVAL_SECONDS:-3}"
BACKFILL_BATCH_PAUSE_SECONDS="${BACKFILL_BATCH_PAUSE_SECONDS:-30}"
BACKFILL_END_DATE="${BACKFILL_END_DATE:-$(python3 -c 'from datetime import date,timedelta; print(date.today()-timedelta(days=1))')}"

if [[ ! -f "${BACKFILL_SNAPSHOT}" ]]; then
  echo "缺少已核验的历史股票清单：${BACKFILL_SNAPSHOT}" >&2
  exit 2
fi
if [[ ! "${BACKFILL_BATCHES}" =~ ^[0-9]+$ ]] || (( BACKFILL_BATCHES < 1 || BACKFILL_BATCHES > 100 )); then
  echo "BACKFILL_BATCHES 须为 1..100。" >&2
  exit 2
fi
export PYTHONPATH="${PROJECT_ROOT}/src${BACKFILL_EXTRA_PYTHONPATH:+:${BACKFILL_EXTRA_PYTHONPATH}}${PYTHONPATH:+:${PYTHONPATH}}"
if ! "${BACKFILL_PYTHON}" -c 'import baostock, pandas' >/dev/null 2>&1; then
  echo "指定的 Python 环境缺少 BaoStock 或 pandas；请配置 BACKFILL_PYTHON 与 BACKFILL_EXTRA_PYTHONPATH。" >&2
  exit 2
fi

for ((batch=1; batch<=BACKFILL_BATCHES; batch++)); do
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] 历史回补批次 ${batch}/${BACKFILL_BATCHES}，最多 10 个三年窗口。"
  if "${BACKFILL_PYTHON}" -m quant_lab.baostock_backfill run \
    --snapshot "${BACKFILL_SNAPSHOT}" \
    --db "${BACKFILL_DB}" \
    --source baostock \
    --start 2000-01-01 \
    --end "${BACKFILL_END_DATE}" \
    --max-windows 10 \
    --interval-seconds "${BACKFILL_INTERVAL_SECONDS}" \
    --result-json "${BACKFILL_LAST_RESULT_OUT}"; then
    :
  else
    result=$?
    "${BACKFILL_PYTHON}" -m quant_lab.archive_publication \
      --db "${BACKFILL_DB}" --out "${BACKFILL_STATUS_OUT}" || true
    exit "${result}"
  fi
  "${BACKFILL_PYTHON}" -m quant_lab.archive_publication \
    --db "${BACKFILL_DB}" --out "${BACKFILL_STATUS_OUT}"
  remaining="$("${BACKFILL_PYTHON}" -c \
    'import json,sys; print(int(json.load(open(sys.argv[1], encoding="utf-8"))["remaining_window_count"]))' \
    "${BACKFILL_LAST_RESULT_OUT}")"
  if (( remaining == 0 )); then
    echo "历史回补已覆盖当前快照与日期范围内的全部计划窗口。"
    break
  fi
  if (( batch < BACKFILL_BATCHES )); then
    sleep "${BACKFILL_BATCH_PAUSE_SECONDS}"
  fi
done
