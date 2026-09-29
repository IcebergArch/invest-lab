#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 Docker CLI。请先安装并启动 Docker Desktop。" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "未找到 Docker Compose。" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker 服务尚不可用。请先启动 Docker Desktop。" >&2
  exit 1
fi
if [[ ! -s data/quant/market.sqlite3 || ! -s reports/quant/latest/run.json ]]; then
  echo "缺少已生成的数据或报告；请先单独运行量化研究命令。启动脚本不会同步或改写数据。" >&2
  exit 1
fi

export QUANT_LOCAL_UID="$(id -u)"
export QUANT_LOCAL_GID="$(id -g)"
export QUANT_DASHBOARD_PORT="${QUANT_DASHBOARD_PORT:-8765}"

COMPOSE=(docker compose -f compose.quant-dashboard.yaml)
"${COMPOSE[@]}" build quant-api visualization
"${COMPOSE[@]}" up -d --no-build quant-api visualization

for ((attempt=1; attempt<=30; attempt++)); do
  if curl --silent --show-error --fail --max-time 2 \
    "http://127.0.0.1:${QUANT_DASHBOARD_PORT}/api/dashboard" >/dev/null 2>&1; then
    echo "可视化服务已启动：http://127.0.0.1:${QUANT_DASHBOARD_PORT}/"
    exit 0
  fi
  sleep 1
done

echo "容器已启动，但健康检查未通过。查看日志：docker compose -f compose.quant-dashboard.yaml logs quant-api visualization" >&2
exit 1
