#!/usr/bin/env bash
# 本机开发启动：默认端口 8089，仅监听 127.0.0.1（与文档约定一致）
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
PORT="${PORT:-8089}"
# shellcheck source=/dev/null
source "$REPO_ROOT/.venv/bin/activate"
echo "智能组货开发服务: http://127.0.0.1:${PORT}/admin"
exec uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload
