#!/usr/bin/env bash
set -euo pipefail
LABEL="com.local.zhinengzuhuo.dev"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UIDSTR="$(id -u)"

if [[ -f "$PLIST_PATH" ]]; then
  launchctl bootout "gui/${UIDSTR}/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "已移除: $PLIST_PATH"
else
  echo "未找到已安装的 plist: $PLIST_PATH"
fi
