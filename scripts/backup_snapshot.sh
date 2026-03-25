#!/usr/bin/env bash
# 创建一次“快照备份”（tar.gz）
#
# 默认备份：代码与文档（不包含 app.db，避免把业务数据打包到公共介质）
# 如需把当前 app.db 也一起备份：增加参数 --include-db
#
# 使用示例：
#   ./scripts/backup_snapshot.sh
#   ./scripts/backup_snapshot.sh --include-db
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/.backups}"

INCLUDE_DB=0
if [[ "${1:-}" == "--include-db" ]]; then
  INCLUDE_DB=1
fi

mkdir -p "$BACKUP_DIR"

ts="$(date +"%Y%m%d_%H%M%S")"
out="${BACKUP_DIR}/pharma-ai_code_${ts}.tar.gz"

echo "创建快照备份：$out"

# 仅打包必要目录；排除虚拟环境与日志/缓存
tar -czf "$out" \
  --exclude=".venv" \
  --exclude="app.db" \
  --exclude="*.log" \
  --exclude=".DS_Store" \
  -C "$REPO_ROOT" \
  app scripts requirements.txt README.md DEPLOY.md DEPLOY_ALIYUN.md API_EXAMPLES.md 2>/dev/null || true

if [[ "$INCLUDE_DB" -eq 1 ]]; then
  echo "同时生成包含 app.db 的备份..."
  out2="${BACKUP_DIR}/pharma-ai_code_plus_db_${ts}.tar.gz"
  tar -czf "$out2" \
    --exclude=".venv" \
    --exclude="*.log" \
    --exclude=".DS_Store" \
    -C "$REPO_ROOT" \
    app scripts requirements.txt README.md DEPLOY.md DEPLOY_ALIYUN.md API_EXAMPLES.md app.db
  echo "包含 app.db 备份完成：$out2"
fi

echo "备份完成：$out"
echo "备份目录：$BACKUP_DIR"

