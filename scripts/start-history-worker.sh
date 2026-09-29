#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

if [[ ! -s reports/quant/snapshots/2026-09-29-baostock-historical-shsz-590b41a3bd0a.json ]]; then
  echo "缺少已核验的历史股票清单。" >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker 服务尚不可用。" >&2
  exit 2
fi

export QUANT_LOCAL_UID="$(id -u)"
export QUANT_LOCAL_GID="$(id -g)"
docker compose -f compose.quant-history.yaml build history-backfill
docker compose -f compose.quant-history.yaml up -d --no-build history-backfill
echo "历史回补容器已启动；查看进度：docker compose -f compose.quant-history.yaml logs -f history-backfill"
